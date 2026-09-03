"""
Regression test: a transient database error at fire time must not permanently
fail a scheduled recording.

Before fix: the per-schedule dispatch handler in _queue_ready_recordings caught
every exception the same way — roll back, mark the schedule FAILED with the raw
exception text, commit. A one-off OperationalError ("database is locked", a
dropped connection) at 8:00pm therefore killed the recording forever, even
though retrying 200ms later would have worked and the program was still inside
the channel's catchup window.

After fix: dispatch of a due schedule is retried a bounded number of times
within the same tick when the failure looks like infrastructure (SQLAlchemy
OperationalError and friends). Deterministic errors — a builder ValueError, bad
program data — still fail the schedule on the first attempt. If every in-tick
attempt hits a transient error, the schedule stays dispatchable (SCHEDULED)
with an explanatory status_message so the next 30-second poll picks it up
again, and no Download row is left behind.
"""
import asyncio
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
from sqlalchemy.exc import OperationalError
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
from services import scheduled_manager as scheduled_manager_module
from services.scheduled_manager import ScheduledManager


def _transient_db_error() -> OperationalError:
    return OperationalError(
        "UPDATE scheduled_recordings SET status=?",
        {},
        Exception("database is locked"),
    )


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


class ScheduleDispatchRetryTests(unittest.IsolatedAsyncioTestCase):
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
        self.build_calls = 0

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _session_ctx(self):
        factory = self.session_factory

        @asynccontextmanager
        async def ctx():
            async with factory() as session:
                yield session

        return ctx

    async def _run_tick(self, build):
        manager = ScheduledManager()
        self.sleep_mock = AsyncMock()
        with (
            patch("services.scheduled_manager.async_session_maker", self._session_ctx()),
            patch("services.scheduled_manager.build_download_from_program", new=build),
            patch("services.scheduled_manager.download_manager", self.dm),
            patch.object(scheduled_manager_module.asyncio, "sleep", self.sleep_mock),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

    async def _fetch_state(self):
        async with self.session_factory() as session:
            downloads = (await session.execute(select(Download))).scalars().all()
            schedule = (await session.execute(select(ScheduledRecording))).scalars().one()
            return downloads, schedule

    async def test_transient_error_on_first_attempt_still_dispatches(self):
        """REGRESSION: a one-off DB error must not lose the recording."""
        async def flaky_build(*args, **kwargs):
            self.build_calls += 1
            if self.build_calls == 1:
                raise _transient_db_error()
            return _make_download()

        await self._run_tick(flaky_build)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(
            schedule.status, ScheduledStatus.QUEUED.value,
            "A transient DB error on the first attempt must be retried, not fatal.",
        )
        self.assertEqual(len(downloads), 1, "Retrying must not duplicate the download row.")
        self.assertEqual(schedule.download_id, downloads[0].id)
        self.assertEqual(self.dm._queue.qsize(), 1)
        self.assertEqual(self.dm._queue.get_nowait(), downloads[0].id)
        self.assertIsNone(schedule.status_message)
        self.assertEqual(self.build_calls, 2, "Exactly one retry should have been needed.")

    async def test_persistent_transient_error_leaves_schedule_dispatchable(self):
        """Every in-tick attempt failing transiently must leave the schedule
        SCHEDULED with an explanation, so the next poll tries again."""
        async def always_flaky(*args, **kwargs):
            self.build_calls += 1
            raise _transient_db_error()

        await self._run_tick(always_flaky)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(
            schedule.status, ScheduledStatus.SCHEDULED.value,
            "A schedule that only hit transient errors must stay dispatchable.",
        )
        self.assertEqual(len(downloads), 0, "A rolled-back attempt must leave no download row.")
        self.assertEqual(self.dm._queue.qsize(), 0)
        self.assertIsNotNone(schedule.status_message)
        self.assertIn("retry", schedule.status_message.lower())
        self.assertEqual(
            self.build_calls,
            scheduled_manager_module.DISPATCH_MAX_ATTEMPTS,
            "Dispatch should be attempted a bounded number of times per tick.",
        )
        self.assertTrue(self.sleep_mock.await_count >= 1, "Retries should back off.")

    async def test_deterministic_error_after_a_transient_one_still_fails(self):
        """A transient first attempt must not turn a later deterministic
        failure back into 'will retry' - that would loop forever on an error
        the user needs to see."""
        async def flaky_then_bad(*args, **kwargs):
            self.build_calls += 1
            if self.build_calls == 1:
                raise _transient_db_error()
            raise ValueError("No catchup URL for this program")

        await self._run_tick(flaky_then_bad)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(
            schedule.status, ScheduledStatus.FAILED.value,
            "A deterministic error ends dispatch even after a transient one.",
        )
        self.assertIn("No catchup URL for this program", schedule.status_message)
        self.assertNotIn("retry", schedule.status_message.lower())
        self.assertEqual(len(downloads), 0)
        self.assertEqual(self.build_calls, 2)

    async def test_non_transient_error_fails_immediately(self):
        """A deterministic error must fail the schedule on the first attempt."""
        async def bad_build(*args, **kwargs):
            self.build_calls += 1
            raise ValueError("No catchup URL for this program")

        await self._run_tick(bad_build)

        downloads, schedule = await self._fetch_state()
        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)
        self.assertEqual(self.build_calls, 1, "A deterministic error must not be retried.")
        self.assertEqual(len(downloads), 0)
        self.assertIn("No catchup URL for this program", schedule.status_message)
        self.assertNotEqual(
            schedule.status_message, "No catchup URL for this program",
            "status_message should read as a sentence, not a bare exception string.",
        )


if __name__ == "__main__":
    unittest.main()
