"""
Regression test: schedule dispatch must be atomic with the download row.

Before fix: _queue_ready_recordings called download_manager.queue_download(),
which committed the Download row on its own session and put it on the work
queue, and only afterwards committed the schedule's QUEUED status on a second
session. If that second commit failed, the download already existed (and ran)
while the schedule stayed dispatchable, so the next 30-second tick dispatched
it again, producing duplicate downloads.

After fix: the Download row is staged on the scheduler's session and committed
in the same transaction as the schedule update. The download is only enqueued
after that single commit succeeds. A commit failure leaves no Download row and
nothing on the queue.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    ScheduledRecording,
    ScheduledStatus,
    XtreamAccount,
)
from services.download_manager import DownloadManager
from services.scheduled_manager import ScheduledManager


def _make_ready_schedule() -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id="101",
        channel_name="Test Channel",
        program_title="Test Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_download() -> Download:
    return Download(
        account_id=1,
        channel_id="101",
        channel_name="Test Channel",
        program_title="Test Show",
        program_start=datetime(2024, 1, 1, 20, 0),
        program_end=datetime(2024, 1, 1, 21, 0),
        duration_minutes=60,
        source_url="http://provider.test/ts",
        output_path="/tmp/downloads/test-show.ts",
        status=DownloadStatus.PENDING.value,
    )


class _FlakyCommitSession:
    """Proxy around a real session that fails the Nth commit call."""

    def __init__(self, inner, state, fail_on_commit):
        self._inner = inner
        self._state = state
        self._fail_on_commit = fail_on_commit

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def commit(self):
        self._state["commits"] += 1
        if self._state["commits"] == self._fail_on_commit:
            await self._inner.rollback()
            raise RuntimeError("simulated commit failure")
        await self._inner.commit()


class ScheduleDispatchAtomicTests(unittest.IsolatedAsyncioTestCase):
    """A failed dispatch commit must leave no Download row and an empty queue."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                name="acc", server_url="http://provider.test",
                username="u", password="p",
            ))
            settings = AppSettings()
            settings.download_folder = "/tmp/downloads"
            settings.min_free_space_gb = 1
            session.add(settings)
            session.add(_make_ready_schedule())
            await session.commit()

        self.dm = DownloadManager()
        self.dm._broadcast_log = AsyncMock()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _session_ctx(self, fail_on_commit=None):
        factory = self.session_factory
        state = {"commits": 0}

        @asynccontextmanager
        async def ctx():
            async with factory() as session:
                if fail_on_commit is None:
                    yield session
                else:
                    yield _FlakyCommitSession(session, state, fail_on_commit)

        return ctx

    async def _run_tick(self, fail_on_commit=None):
        async def fake_build(*args, **kwargs):
            return _make_download()

        manager = ScheduledManager()
        with (
            patch("services.scheduled_manager.async_session_maker",
                  self._session_ctx(fail_on_commit)),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", self.dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

    async def _fetch_state(self):
        async with self.session_factory() as session:
            downloads = (await session.execute(select(Download))).scalars().all()
            schedule = (await session.execute(select(ScheduledRecording))).scalars().one()
            return downloads, schedule

    async def test_failed_dispatch_commit_leaves_no_download(self):
        """REGRESSION: when the dispatch commit fails, no Download row may exist
        and nothing may sit on the download queue.

        Commit order in a tick: (1) classification marks, (2) dispatch commit
        for the schedule + staged download, (3) FAILED mark after the error.
        Failing commit #2 simulates the dispatch commit failure.
        """
        await self._run_tick(fail_on_commit=2)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(
            len(downloads), 0,
            "A failed dispatch commit must not leave an orphan Download row.",
        )
        self.assertEqual(
            self.dm._queue.qsize(), 0,
            "A failed dispatch commit must not enqueue the download.",
        )
        self.assertNotEqual(
            schedule.status, ScheduledStatus.QUEUED.value,
            "Schedule must not report QUEUED when its dispatch commit failed.",
        )

    async def test_successful_dispatch_commits_both_rows(self):
        """Sanity: a normal dispatch persists the download and schedule together."""
        await self._run_tick(fail_on_commit=None)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].status, DownloadStatus.PENDING.value)
        self.assertEqual(schedule.status, ScheduledStatus.QUEUED.value)
        self.assertEqual(schedule.download_id, downloads[0].id)
        self.assertEqual(self.dm._queue.qsize(), 1)
        self.assertEqual(self.dm._queue.get_nowait(), downloads[0].id)


if __name__ == "__main__":
    unittest.main()
