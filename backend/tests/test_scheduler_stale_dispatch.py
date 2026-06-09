import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, ScheduledRecording, ScheduledStatus
from services.scheduled_manager import ScheduledManager


def _make_stale_schedule(hours_ago: int = 48) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(hours=hours_ago)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id="101",
        channel_name="Test Channel",
        program_title="Old Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_schedule_with_padding(post_padding_minutes: int, stop_offset_seconds: int) -> ScheduledRecording:
    """Create a schedule whose program ended `stop_offset_seconds` seconds ago with post-padding."""
    now = datetime.now(timezone.utc)
    end = now - timedelta(seconds=stop_offset_seconds)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id="103",
        channel_name="Test Channel",
        program_title="Padded Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        post_padding_minutes=post_padding_minutes,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_recent_schedule(minutes_ago: int = 5) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=minutes_ago)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id="102",
        channel_name="Test Channel",
        program_title="Recent Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedule, settings_obj=None):
    scalars_result = MagicMock()
    scalars_result.all.return_value = [schedule]

    schedules_exec_result = MagicMock()
    schedules_exec_result.scalars.return_value = scalars_result

    if settings_obj is None:
        settings_obj = AppSettings()
        settings_obj.download_folder = "/tmp/downloads"
        settings_obj.min_free_space_gb = 1

    settings_exec_result = MagicMock()
    settings_exec_result.scalar_one_or_none.return_value = settings_obj

    call_count = [0]

    async def execute(stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return schedules_exec_result
        return settings_exec_result

    session = AsyncMock()
    session.execute = execute
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


class StaleScheduleDispatchTests(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests for stale schedule dispatch.

    _queue_ready_recordings marks a schedule as FAILED only when
    available_at is older than the channel's actual catchup window
    (fetched from the provider). Leaves the schedule in SCHEDULED state
    when the provider is unreachable so it retries next poll.
    """

    async def _run_queue(self, schedule, archive_days: int = 1) -> list:
        """Run _queue_ready_recordings and return list of dispatched download IDs."""
        queued: list = []

        async def fake_build(*args, **kwargs):
            dl = MagicMock()
            dl.id = 99
            return dl

        async def fake_queue(dl):
            queued.append(dl.id)
            return dl

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=fake_queue)

        manager = ScheduledManager()
        session_maker = _make_session_maker(schedule)

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(
                manager,
                "_get_catchup_window_days",
                new=AsyncMock(return_value=archive_days),
            ),
        ):
            await manager._queue_ready_recordings()

        return queued

    async def test_past_catchup_window_not_dispatched(self):
        """Schedule older than the provider's catchup window must not be dispatched."""
        schedule = _make_stale_schedule(hours_ago=48)
        dispatched = await self._run_queue(schedule, archive_days=1)

        self.assertEqual(
            dispatched,
            [],
            "Schedule that ended 48 hours ago must not dispatch on a 1-day catchup window.",
        )

    async def test_within_catchup_window_still_dispatched(self):
        """Schedule within a multi-day catchup window must dispatch even if it ended 2 days ago."""
        schedule = _make_stale_schedule(hours_ago=48)
        dispatched = await self._run_queue(schedule, archive_days=7)

        self.assertEqual(
            dispatched,
            [99],
            "A schedule that ended 2 days ago must still dispatch when the channel has a 7-day catchup window.",
        )

    async def test_stale_schedule_marked_failed_with_catchup_message(self):
        """Stale schedule must be marked FAILED with a message that mentions catchup."""
        schedule = _make_stale_schedule(hours_ago=48)
        await self._run_queue(schedule, archive_days=1)

        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)
        self.assertIsNotNone(schedule.status_message)
        self.assertIn(
            "catchup",
            schedule.status_message.lower(),
            "status_message must explain the catchup window, not a raw provider error.",
        )

    async def test_post_padding_does_not_rescue_expired_recording(self):
        """post_padding must not push an expired recording back inside the catchup window.

        Program ended 1 day + 1 second ago (expired on a 1-day window). With 30 minutes
        of post-padding, available_at falls just inside the boundary — but the program
        itself is expired and must be marked FAILED, not dispatched.
        """
        archive_days = 1
        # Program ended 1 second past the archive boundary
        stop_offset = int(timedelta(days=archive_days).total_seconds()) + 1
        schedule = _make_schedule_with_padding(post_padding_minutes=30, stop_offset_seconds=stop_offset)
        dispatched = await self._run_queue(schedule, archive_days=archive_days)

        self.assertEqual(
            dispatched,
            [],
            "Expired recording must not dispatch even when post_padding shifts available_at inside the window.",
        )
        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)

    async def test_recent_schedule_still_dispatched(self):
        """Sanity: a schedule whose show ended 5 minutes ago must still fire."""
        schedule = _make_recent_schedule(minutes_ago=5)
        dispatched = await self._run_queue(schedule, archive_days=1)

        self.assertEqual(
            dispatched,
            [99],
            "A schedule for a show that ended 5 minutes ago must be dispatched.",
        )

    async def test_expired_schedule_failed_status_committed(self):
        """REGRESSION: Expired schedule FAILED status must be committed to DB.

        When only expired schedules exist, _queue_ready_recordings hits
        'if not ready: return' (line 82) before calling session.commit().
        SQLAlchemy discards the uncommitted FAILED status on session close,
        leaving the schedule permanently SCHEDULED in the DB.

        This test fails before the fix and passes after.
        """
        schedule = _make_stale_schedule(hours_ago=48)

        # Build the session mock directly so we can inspect commit() calls.
        scalars_result = MagicMock()
        scalars_result.all.return_value = [schedule]
        schedules_exec_result = MagicMock()
        schedules_exec_result.scalars.return_value = scalars_result

        settings_obj = AppSettings()
        settings_obj.download_folder = "/tmp/downloads"
        settings_obj.min_free_space_gb = 1
        settings_exec_result = MagicMock()
        settings_exec_result.scalar_one_or_none.return_value = settings_obj

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return schedules_exec_result
            return settings_exec_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        @asynccontextmanager
        async def session_ctx():
            yield session

        manager = ScheduledManager()

        with (
            patch("services.scheduled_manager.async_session_maker", session_ctx),
            patch.object(
                manager,
                "_get_catchup_window_days",
                new=AsyncMock(return_value=1),
            ),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            schedule.status,
            ScheduledStatus.FAILED.value,
            "Expired schedule must be marked FAILED.",
        )
        # FAILS before fix: commit is skipped by "if not ready: return"
        session.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
