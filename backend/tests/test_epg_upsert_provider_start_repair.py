"""
Regression test: EPG rows missing provider_start are never repaired by re-ingest.

Bug: _program_insert_stmt() upserts on (account_id, epg_id) but its
ON CONFLICT DO UPDATE set only covered title, description, and category.
A guide row created without provider_start (an API backfill entry that had
no "start" field, or a legacy row predating the provider_start column)
kept provider_start = NULL forever, even when a later XMLTV refresh carried
the provider-local start for the very same programme.

User impact: download_builder._resolve_provider_start() returns None for
those rows, so build_timeshift_url() falls back to formatting the UTC start
time into the timeshift URL. For any provider whose wall clock is not UTC,
the download fetches the wrong time window (the show is shifted by the
provider's UTC offset).

Fix: the upsert now updates provider_start/provider_stop with
COALESCE(excluded.provider_start, provider_start) so a re-ingest that has
the value repairs the row, while a re-ingest that lacks it cannot erase an
existing good value.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import models  # noqa: F401  # import all models so Base.metadata includes every table
from database import Base
from models import EPGProgram
from services.epg_ingest_manager import _program_insert_stmt


def _program_row(epg_id, provider_start=None, provider_stop=None, title="Show"):
    start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    return {
        "account_id": 1,
        "epg_id": epg_id,
        "channel_id": "101",
        "channel_name": "Test Channel",
        "title": title,
        "description": None,
        "category": None,
        "start_time": start,
        "end_time": end,
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(end.timestamp()),
        "duration_minutes": 60,
        "has_archive": True,
        "provider_start": provider_start,
        "provider_stop": provider_stop,
        "xmltv_id": None,
    }


class UpsertProviderStartRepairTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _insert(self, rows):
        async with self.session_maker() as session:
            async with session.begin():
                await session.execute(_program_insert_stmt(), rows)

    async def _fetch(self, epg_id):
        async with self.session_maker() as session:
            result = await session.execute(
                select(EPGProgram).where(
                    EPGProgram.account_id == 1,
                    EPGProgram.epg_id == epg_id,
                )
            )
            return result.scalar_one_or_none()

    async def test_null_provider_start_repaired_by_later_ingest(self):
        """A row created without provider_start must pick it up from a later
        ingest of the same programme (e.g. XMLTV refresh after an API backfill
        whose entry lacked the "start" field)."""
        epg_id = "101:1780315200:1780318800"
        await self._insert([_program_row(epg_id, provider_start=None, provider_stop=None)])
        await self._insert([
            _program_row(
                epg_id,
                provider_start="20260601140000 +0200",
                provider_stop="20260601150000 +0200",
            )
        ])

        prog = await self._fetch(epg_id)
        self.assertEqual(
            prog.provider_start,
            "20260601140000 +0200",
            "Re-ingest carrying provider_start must repair a row that was stored "
            "without it; otherwise the timeshift URL falls back to UTC and "
            "downloads the wrong time window.",
        )
        self.assertEqual(prog.provider_stop, "20260601150000 +0200")

    async def test_existing_provider_start_not_erased_by_null_ingest(self):
        """A re-ingest without provider_start (sparse API backfill entry) must
        not wipe an existing good provider-local value."""
        epg_id = "101:1780315200:1780318800"
        await self._insert([
            _program_row(
                epg_id,
                provider_start="20260601140000 +0200",
                provider_stop="20260601150000 +0200",
            )
        ])
        await self._insert([_program_row(epg_id, provider_start=None, provider_stop=None)])

        prog = await self._fetch(epg_id)
        self.assertEqual(
            prog.provider_start,
            "20260601140000 +0200",
            "An upsert with NULL provider_start must keep the existing value.",
        )
        self.assertEqual(prog.provider_stop, "20260601150000 +0200")

    async def test_non_null_provider_start_updated_by_later_value(self):
        """A re-ingest with a (possibly corrected) provider_start wins."""
        epg_id = "101:1780315200:1780318800"
        await self._insert([_program_row(epg_id, provider_start="20260601130000 +0100")])
        await self._insert([_program_row(epg_id, provider_start="20260601140000 +0200")])

        prog = await self._fetch(epg_id)
        self.assertEqual(prog.provider_start, "20260601140000 +0200")


if __name__ == "__main__":
    unittest.main()
