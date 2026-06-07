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
    BUG: _queue_ready_recordings dispatches any SCHEDULED/PAUSED_LOW_SPACE
    recording where available_at <= now_utc with no upper-bound check. After
    a server outage or disk-full pause longer than the provider's catchup
    window, stale schedules fire unconditionally. The download fails with a
    raw provider error and gives the user no actionable context.

    These tests currently FAIL because stale schedules are dispatched.
    After the fix (add an expiry guard before dispatching) they pass.
    """

    async def _run_queue(self, schedule) -> list:
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
        ):
            await manager._queue_ready_recordings()

        return queued

    async def test_48_hour_old_schedule_not_dispatched(self):
        """
        A schedule whose stop_timestamp is 48 hours in the past must NOT be
        dispatched: the provider's catchup window (typically 24-48 hours) has
        almost certainly expired.

        Currently FAILS: the schedule IS dispatched unconditionally regardless
        of age. Fix: add an expiry guard in _queue_ready_recordings.
        """
        schedule = _make_stale_schedule(hours_ago=48)
        dispatched = await self._run_queue(schedule)

        self.assertEqual(
            dispatched,
            [],
            "Schedule that ended 48 hours ago must not be dispatched. "
            "Currently _queue_ready_recordings fires it unconditionally, "
            "producing a FAILED download with a cryptic provider error.",
        )

    async def test_48_hour_old_schedule_marked_failed_not_silent(self):
        """
        A stale schedule must be marked FAILED with a plain-English message,
        not silently left in SCHEDULED status indefinitely.

        Currently FAILS: the stale schedule is dispatched (wrong), or left
        in SCHEDULED with no change.
        Fix: mark status=FAILED with a human-readable status_message.
        """
        schedule = _make_stale_schedule(hours_ago=48)
        await self._run_queue(schedule)

        self.assertIn(
            schedule.status,
            [ScheduledStatus.FAILED.value, ScheduledStatus.CANCELLED.value],
            f"Stale schedule must end up FAILED or CANCELLED with a user-readable "
            f"status_message. Currently status is '{schedule.status}'.",
        )
        self.assertIsNotNone(
            schedule.status_message,
            "status_message must explain why the schedule was not dispatched "
            "(e.g. 'Program too old for catchup').",
        )

    async def test_recent_schedule_still_dispatched(self):
        """Sanity: a schedule whose show ended 5 minutes ago must still fire."""
        schedule = _make_recent_schedule(minutes_ago=5)
        dispatched = await self._run_queue(schedule)

        self.assertEqual(
            dispatched,
            [99],
            "A schedule for a show that ended 5 minutes ago must be dispatched.",
        )


if __name__ == "__main__":
    unittest.main()
