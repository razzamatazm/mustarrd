"""
Regression test: session.rollback() during dispatch of schedule A must not
mark schedule B FAILED via SQLAlchemy attribute expiry (MissingGreenlet).

When two schedules are ready in the same tick and A's queue_download fails
after starting a DB transaction, session.rollback() expires all SQLAlchemy
objects in the identity map. Without the fix, the next loop iteration reads
B's expired attributes, raises MissingGreenlet in async context, and the
generic except catches it and marks B FAILED with a cryptic error message.

With the fix, schedule attributes are snapshotted into local variables before
the dispatch try block, so the rollback's expiry has no effect on B's data.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    ScheduledRecording,
    ScheduledStatus,
    XtreamAccount,
)
from services.scheduled_manager import ScheduledManager


def _make_schedule(channel_id: str, title: str) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id=channel_id,
        channel_name=f"Channel {channel_id}",
        program_title=title,
        program_description="Test description",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_download(call_num: int) -> Download:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Download(
        account_id=1,
        channel_id=f"ch{call_num}",
        channel_name="Test Channel",
        program_title=f"Show {call_num}",
        program_start=now,
        program_end=now,
        start_timestamp=0,
        stop_timestamp=0,
        duration_minutes=60,
        source_url="http://provider/stream.ts",
        output_path=f"/tmp/test_show_{call_num}.ts",
    )


class DispatchRollbackExpiryTests(unittest.IsolatedAsyncioTestCase):
    """Rollback on A (with active transaction) must not corrupt B via expiry."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # expire_on_commit=False matches the production session factory: only
        # rollback (not commit) can expire objects.
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.Session() as session:
            session.add(XtreamAccount(
                id=1, name="acc", server_url="http://test",
                username="u", password="p",
            ))
            session.add(AppSettings(
                download_folder="/tmp/dl",
                completed_folder="/tmp/done",
                min_free_space_gb=1,
            ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _add_schedules(self, *schedules):
        async with self.Session() as session:
            for s in schedules:
                session.add(s)
            await session.commit()
            for s in schedules:
                await session.refresh(s)

    async def test_b_dispatches_after_a_queue_fails(self):
        """B is QUEUED even when A's queue_download raises after starting a DB
        transaction, which causes session.rollback() to expire B's attributes."""
        sched_a = _make_schedule("ch1", "Show A")
        sched_b = _make_schedule("ch2", "Show B")
        await self._add_schedules(sched_a, sched_b)
        a_id, b_id = sched_a.id, sched_b.id

        build_call = [0]
        queue_call = [0]

        async def fake_build(session, **kwargs):
            build_call[0] += 1
            return _make_download(build_call[0])

        async def fake_queue(dl, session=None):
            queue_call[0] += 1
            if queue_call[0] == 1 and session is not None:
                # Simulate real queue_download: execute a DB statement so the
                # session has an active transaction, then raise. This causes the
                # scheduler's session.rollback() to expire all session objects
                # (including B), reproducing the MissingGreenlet bug.
                await session.execute(text("SELECT 1"))
                raise Exception("Simulated queue failure for A")
            return dl

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=fake_queue)
        fake_dm.enqueue_persisted = AsyncMock(side_effect=lambda dl: dl)

        manager = ScheduledManager()

        @asynccontextmanager
        async def session_maker():
            async with self.Session() as session:
                yield session

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

        async with self.Session() as session:
            a_db = (await session.execute(
                select(ScheduledRecording).where(ScheduledRecording.id == a_id)
            )).scalar_one()
            b_db = (await session.execute(
                select(ScheduledRecording).where(ScheduledRecording.id == b_id)
            )).scalar_one()

        self.assertEqual(
            a_db.status,
            ScheduledStatus.FAILED.value,
            "Schedule A must be FAILED because its queue raised an exception.",
        )
        self.assertEqual(
            b_db.status,
            ScheduledStatus.QUEUED.value,
            f"Schedule B must be QUEUED after A fails. "
            f"Got status={b_db.status!r}, message={b_db.status_message!r}",
        )
        self.assertNotIn(
            "MissingGreenlet",
            b_db.status_message or "",
            "B must not be marked FAILED with a SQLAlchemy internal error.",
        )
