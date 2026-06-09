"""
Regression test: interrupted backfill loses already-fetched channels' progress.

Bug: _backfill_from_api accumulated rows in a shared batch that was only
flushed to the database every 1000 entries and once at the very end. When
the backfill was interrupted partway (app restart, task cancellation, an
unhandled error), every entry still sitting in the pending batch was lost —
for typical channels with a few hundred guide entries each, that meant the
last several fully-fetched channels were thrown away. Since
last_epg_backfill_at is also not set on interrupt, the next refresh
re-triggered the backfill and had to re-fetch those channels from the
provider all over again.

Fix: the batch is flushed (committed) after each channel completes, so each
channel's progress is durable. On restart, channels whose rows were
persisted drop out of the backfill targets and the backfill resumes from
where it left off instead of redoing everything.
"""
import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import models  # noqa: F401  # import all models so Base.metadata includes every table
from database import Base
from models import EPGProgram
from services.epg_ingest_manager import EPGIngestManager, _program_insert_stmt


def _make_entry(now_utc, hours_ago):
    start = now_utc - timedelta(hours=hours_ago)
    stop = start + timedelta(hours=1)
    return {
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(stop.timestamp()),
        "title": "U2hvdw==",  # base64 "Show"
        "description": None,
        "has_archive": 1,
    }


def _channel_target(stream_id, now_utc, archive_days=7):
    return (
        {"stream_id": stream_id, "name": f"Channel {stream_id}", "tv_archive": 1},
        now_utc,
        archive_days,
    )


class _InterruptingClient:
    """Returns entries for channel 101, then simulates an interrupt on 102."""

    def __init__(self, now_utc):
        self._now_utc = now_utc

    async def get_epg(self, stream_id):
        if str(stream_id) == "101":
            return [_make_entry(self._now_utc, hours_ago=h) for h in (3, 2)]
        raise asyncio.CancelledError()


class BackfillInterruptProgressTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.manager = EPGIngestManager()
        self.now_utc = datetime.now(timezone.utc)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _program_count(self):
        async with self.session_maker() as session:
            result = await session.execute(select(EPGProgram))
            return len(result.scalars().all())

    async def test_completed_channels_survive_interrupt(self):
        """Rows for a fully-fetched channel must be committed before the next
        channel is fetched, so an interrupt does not lose that progress."""
        client = _InterruptingClient(self.now_utc)
        targets = [
            _channel_target("101", self.now_utc),
            _channel_target("102", self.now_utc),
        ]

        with patch("services.epg_ingest_manager.async_session_maker", self.session_maker):
            with patch("services.epg_ingest_manager.backend_log_stream") as mock_stream:
                mock_stream.emit = AsyncMock()
                with self.assertRaises(asyncio.CancelledError):
                    await self.manager._backfill_from_api(
                        client=client,
                        channel_targets=targets,
                        now_utc=self.now_utc,
                        processed=0,
                        inserted=0,
                        account_id=1,
                        insert_stmt=_program_insert_stmt(),
                    )

        count = await self._program_count()
        self.assertEqual(
            count,
            2,
            f"Channel 101's 2 entries must be committed before channel 102 is "
            f"fetched; got {count} rows after the interrupt. Losing them forces "
            f"the next refresh to re-fetch already-backfilled channels.",
        )


if __name__ == "__main__":
    unittest.main()
