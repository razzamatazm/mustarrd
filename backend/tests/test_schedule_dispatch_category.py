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


def _make_ready_schedule(channel_category_name=None) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        account_id=1,
        channel_id="101",
        channel_name="Test Channel",
        channel_category_name=channel_category_name,
        program_title="The Dark Knight",
        program_description="A superhero movie.",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


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


class ScheduleDispatchCategoryTests(unittest.IsolatedAsyncioTestCase):
    """
    Regression for: Scheduled dispatch loses channel category, causing wrong
    filename template when custom_filename is null.

    build_download_from_program reads program["category"] to detect program
    type (movie/sports/news/tv_show). _queue_ready_recordings must include
    "category" from the stored channel_category_name.
    """

    async def _run_queue_capture_program(self, schedule) -> dict:
        captured = {}

        async def fake_build(session, **kwargs):
            captured["program"] = kwargs.get("program", {})
            dl = MagicMock()
            dl.id = 99
            return dl

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=lambda dl: dl)

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
                new=AsyncMock(return_value=7),
            ),
        ):
            await manager._queue_ready_recordings()

        return captured.get("program", {})

    async def test_category_included_in_dispatch_program(self):
        """channel_category_name must appear as 'category' in the program dict at dispatch."""
        schedule = _make_ready_schedule(channel_category_name="Movies")
        program = await self._run_queue_capture_program(schedule)

        self.assertIn(
            "category",
            program,
            "program dict passed to build_download_from_program must include 'category'",
        )
        self.assertEqual(
            program["category"],
            "Movies",
            "program['category'] must match the stored channel_category_name",
        )

    async def test_null_category_sends_empty_string(self):
        """When channel_category_name is None, category must be empty string, not None."""
        schedule = _make_ready_schedule(channel_category_name=None)
        program = await self._run_queue_capture_program(schedule)

        self.assertEqual(
            program.get("category"),
            "",
            "program['category'] must be '' when channel_category_name is None",
        )


if __name__ == "__main__":
    unittest.main()
