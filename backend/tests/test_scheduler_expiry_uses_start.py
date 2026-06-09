"""
Regression: scheduler expiry check compared program END against the catchup
boundary.  A program whose end fell inside the window but whose start fell
outside was dispatched, then failed at the provider with a cryptic 404.

Fix: compare padded program START against the boundary.
"""
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


def _make_session_maker(schedule):
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
    async def ctx():
        yield session

    return ctx


async def _run_queue(schedule, archive_days: int) -> list:
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


class SchedulerExpiryUsesStartTests(unittest.IsolatedAsyncioTestCase):

    async def test_start_outside_window_end_inside_not_dispatched(self):
        """Program started outside the catchup window must be FAILED, not dispatched.

        Before the fix: expiry check used program END, so a long show whose end
        fell inside the window slipped through and dispatched to the provider, which
        returned 404.
        """
        now = datetime.now(timezone.utc)
        archive_days = 7
        # Start is 2 hours past the boundary; end is 30 minutes inside the boundary.
        prog_start = now - timedelta(days=archive_days, hours=2)
        prog_end = now - timedelta(days=archive_days) + timedelta(minutes=30)
        duration_minutes = int((prog_end - prog_start).total_seconds() / 60)

        schedule = ScheduledRecording(
            account_id=1,
            channel_id="ch1",
            channel_name="Test",
            program_title="Boundary Show",
            program_start=prog_start.replace(tzinfo=None),
            program_end=prog_end.replace(tzinfo=None),
            start_timestamp=int(prog_start.timestamp()),
            stop_timestamp=int(prog_end.timestamp()),
            duration_minutes=duration_minutes,
            pre_padding_minutes=0,
            post_padding_minutes=0,
            status=ScheduledStatus.SCHEDULED.value,
        )

        dispatched = await _run_queue(schedule, archive_days=archive_days)

        self.assertEqual(dispatched, [], "Schedule with start outside window must not dispatch.")
        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)
        self.assertIn("catchup", schedule.status_message.lower())

    async def test_start_inside_window_dispatched(self):
        """Program that started inside the catchup window must dispatch normally."""
        now = datetime.now(timezone.utc)
        archive_days = 7
        prog_end = now - timedelta(hours=1)
        prog_start = prog_end - timedelta(hours=1)

        schedule = ScheduledRecording(
            account_id=1,
            channel_id="ch2",
            channel_name="Test",
            program_title="Recent Show",
            program_start=prog_start.replace(tzinfo=None),
            program_end=prog_end.replace(tzinfo=None),
            start_timestamp=int(prog_start.timestamp()),
            stop_timestamp=int(prog_end.timestamp()),
            duration_minutes=60,
            pre_padding_minutes=0,
            post_padding_minutes=0,
            status=ScheduledStatus.SCHEDULED.value,
        )

        dispatched = await _run_queue(schedule, archive_days=archive_days)

        self.assertEqual(dispatched, [99], "Schedule with start inside window must dispatch.")

    async def test_pre_padding_outside_window_not_dispatched(self):
        """Pre-padding that extends past the catchup boundary must prevent dispatch.

        If the padded start falls outside the window the provider cannot serve
        that portion of the recording.
        """
        now = datetime.now(timezone.utc)
        archive_days = 7
        # Program start is 15 minutes inside the window; pre-padding of 30 min
        # shifts the padded start 15 minutes outside the boundary.
        prog_start = now - timedelta(days=archive_days) + timedelta(minutes=15)
        prog_end = prog_start + timedelta(hours=1)

        schedule = ScheduledRecording(
            account_id=1,
            channel_id="ch3",
            channel_name="Test",
            program_title="Padded Show",
            program_start=prog_start.replace(tzinfo=None),
            program_end=prog_end.replace(tzinfo=None),
            start_timestamp=int(prog_start.timestamp()),
            stop_timestamp=int(prog_end.timestamp()),
            duration_minutes=60,
            pre_padding_minutes=30,
            post_padding_minutes=0,
            status=ScheduledStatus.SCHEDULED.value,
        )

        dispatched = await _run_queue(schedule, archive_days=archive_days)

        self.assertEqual(dispatched, [], "Pre-padding outside window must prevent dispatch.")
        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)

    async def test_start_timestamp_zero_falls_back_to_duration(self):
        """When start_timestamp is 0, start is estimated from end minus duration_minutes."""
        now = datetime.now(timezone.utc)
        archive_days = 7
        prog_end = now - timedelta(days=archive_days) + timedelta(minutes=30)
        # 150-minute show: estimated start = prog_end - 150 min = 2 hours outside window
        duration_minutes = 150

        schedule = ScheduledRecording(
            account_id=1,
            channel_id="ch4",
            channel_name="Test",
            program_title="Long Show",
            program_start=(prog_end - timedelta(minutes=duration_minutes)).replace(tzinfo=None),
            program_end=prog_end.replace(tzinfo=None),
            start_timestamp=0,
            stop_timestamp=int(prog_end.timestamp()),
            duration_minutes=duration_minutes,
            pre_padding_minutes=0,
            post_padding_minutes=0,
            status=ScheduledStatus.SCHEDULED.value,
        )

        dispatched = await _run_queue(schedule, archive_days=archive_days)

        self.assertEqual(dispatched, [], "Estimated start outside window must not dispatch.")
        self.assertEqual(schedule.status, ScheduledStatus.FAILED.value)


if __name__ == "__main__":
    unittest.main()
