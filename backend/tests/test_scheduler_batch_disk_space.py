"""
Regression test: a batch of due schedules must not collectively bypass the
minimum free space threshold.

Before fix: _queue_ready_recordings read free disk space once per tick and
compared the same value against min_free_space_gb for every ready schedule.
N schedules coming due in one tick all passed the check (none had written any
bytes yet), so the batch could dispatch far past the threshold.

After fix: each dispatched schedule's expected size (duration + padding at
ESTIMATED_RECORDING_GB_PER_HOUR) is subtracted from the projected free space
before checking the next schedule in the same tick.
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

from models import AppSettings, ScheduledRecording, ScheduledStatus
from services.scheduled_manager import ScheduledManager, ESTIMATED_RECORDING_GB_PER_HOUR

MIN_FREE_GB = 1


def _make_ready_schedule(schedule_id: int) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        id=schedule_id,
        account_id=1,
        channel_id=str(100 + schedule_id),
        channel_name="Test Channel",
        program_title=f"Show {schedule_id}",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedules):
    scalars_result = MagicMock()
    scalars_result.all.return_value = schedules
    schedules_exec_result = MagicMock()
    schedules_exec_result.scalars.return_value = scalars_result

    settings_obj = AppSettings()
    settings_obj.download_folder = "/tmp/downloads"
    settings_obj.min_free_space_gb = MIN_FREE_GB
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
    session.refresh = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


class SchedulerBatchDiskSpaceTests(unittest.IsolatedAsyncioTestCase):
    """The per-schedule disk check must account for in-tick dispatches."""

    async def _run_tick(self, schedules, free_gb):
        async def fake_build(*args, **kwargs):
            dl = MagicMock()
            dl.id = 99
            return dl

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=lambda dl, session=None: dl)
        fake_dm.enqueue_persisted = AsyncMock(side_effect=lambda dl: dl)

        manager = ScheduledManager()
        with (
            patch("services.scheduled_manager.async_session_maker", _make_session_maker(schedules)),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=free_gb),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

    async def test_batch_does_not_bypass_min_free_space(self):
        """REGRESSION: with room for roughly 1.5 recordings above the minimum
        free space, a batch of 3 one-hour schedules must dispatch 2 and pause
        the third, not dispatch all 3."""
        per_recording_gb = ESTIMATED_RECORDING_GB_PER_HOUR  # 60-minute schedules
        free_gb = MIN_FREE_GB + 1.5 * per_recording_gb

        schedules = [_make_ready_schedule(i) for i in (1, 2, 3)]
        await self._run_tick(schedules, free_gb=free_gb)

        statuses = [s.status for s in schedules]
        self.assertEqual(
            statuses[:2],
            [ScheduledStatus.QUEUED.value, ScheduledStatus.QUEUED.value],
            "The first two schedules fit above the threshold and must dispatch.",
        )
        self.assertEqual(
            statuses[2],
            ScheduledStatus.PAUSED_LOW_SPACE.value,
            "The third schedule must be paused: the first two dispatches already "
            "consume the projected free space down to the minimum.",
        )

    async def test_plenty_of_space_dispatches_all(self):
        """Sanity: with ample space the whole batch still dispatches."""
        schedules = [_make_ready_schedule(i) for i in (1, 2, 3)]
        await self._run_tick(schedules, free_gb=1000.0)

        for schedule in schedules:
            self.assertEqual(schedule.status, ScheduledStatus.QUEUED.value)

    async def test_padding_counts_toward_projection(self):
        """Pre/post padding extends the expected recording size."""
        schedule = _make_ready_schedule(1)
        schedule.pre_padding_minutes = 30
        schedule.post_padding_minutes = 30
        manager = ScheduledManager()
        self.assertAlmostEqual(
            manager._estimate_recording_gb(schedule),
            2.0 * ESTIMATED_RECORDING_GB_PER_HOUR,
        )


if __name__ == "__main__":
    unittest.main()
