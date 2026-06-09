"""
Regression tests: the free-space check must not create a missing download folder.

scheduled_manager._get_free_space_gb called os.makedirs(path) when the folder
did not exist. With a download folder on a network mount (e.g. /mnt/nas), an
unmounted share meant the path was silently created on the container's root
filesystem, the free-space check passed against the WRONG disk, and scheduled
recordings then filled the root disk.

Fix: the probe is strictly read-only. A missing or unreachable folder reports
the check as unavailable (None), and the scheduler holds the recording in
PAUSED_LOW_SPACE until the folder comes back instead of dispatching it.
"""
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, ScheduledRecording, ScheduledStatus
from services.disk_space import get_free_space_gb
from services.scheduled_manager import ScheduledManager


def _make_ready_schedule() -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        id=11,
        account_id=1,
        channel_id="200",
        channel_name="Channel",
        program_title="Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedule, download_folder):
    scalars_result = MagicMock()
    scalars_result.all.return_value = [schedule]
    schedules_exec_result = MagicMock()
    schedules_exec_result.scalars.return_value = scalars_result

    settings_obj = AppSettings()
    settings_obj.download_folder = download_folder
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
    session.refresh = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx, session


class FreeSpaceProbeReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    """get_free_space_gb must never create the folder it is asked about."""

    async def test_missing_folder_reports_unavailable_and_is_not_created(self):
        missing = os.path.join(tempfile.gettempdir(), "mustarrd_unmounted_nas", "recordings")
        shutil.rmtree(os.path.dirname(missing), ignore_errors=True)
        self.assertFalse(os.path.exists(missing))

        with patch("os.makedirs", side_effect=AssertionError(
            "free-space check must never create the missing folder"
        )):
            result = await get_free_space_gb(missing)

        self.assertIsNone(
            result,
            "Missing folder must report the check as unavailable (None), not "
            "measure whatever disk currently backs the path.",
        )
        self.assertFalse(
            os.path.exists(missing),
            "Read-only check created the missing folder; on an unmounted NAS "
            "this lands on the container's root filesystem.",
        )

    async def test_existing_folder_reports_free_space(self):
        folder = tempfile.mkdtemp(prefix="free_space_")
        try:
            result = await get_free_space_gb(folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0.0)

    async def test_oserror_reports_unavailable(self):
        with (
            patch("services.disk_space.os.path.exists", return_value=True),
            patch("services.disk_space.shutil.disk_usage",
                  side_effect=OSError(5, "Input/output error")),
        ):
            result = await get_free_space_gb("/mnt/flaky")
        self.assertIsNone(result, "An unreadable mount must report unavailable, not crash.")


class SchedulerMissingFolderTests(unittest.IsolatedAsyncioTestCase):
    """Scheduled recordings must wait, not record onto the wrong disk."""

    async def test_missing_download_folder_pauses_schedule(self):
        missing = os.path.join(tempfile.gettempdir(), "mustarrd_unmounted_nas", "recordings")
        shutil.rmtree(os.path.dirname(missing), ignore_errors=True)

        schedule = _make_ready_schedule()
        session_maker, session = _make_session_maker(schedule, missing)
        manager = ScheduledManager()

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock()

        async def archive_days(sess, sched):
            return 7

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_catchup_window_days", new=archive_days),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            schedule.status,
            ScheduledStatus.PAUSED_LOW_SPACE.value,
            "Missing download folder must hold the schedule, not dispatch it.",
        )
        self.assertIsNotNone(schedule.status_message)
        fake_dm.queue_download.assert_not_called()
        self.assertFalse(
            os.path.exists(missing),
            "Scheduler must not create the missing download folder.",
        )

    async def test_existing_folder_with_space_dispatches_schedule(self):
        folder = tempfile.mkdtemp(prefix="sched_dl_")
        schedule = _make_ready_schedule()
        session_maker, session = _make_session_maker(schedule, folder)
        manager = ScheduledManager()

        download = MagicMock()
        download.id = 77
        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(return_value=download)
        fake_dm.enqueue_persisted = AsyncMock()

        async def archive_days(sess, sched):
            return 7

        try:
            with (
                patch("services.scheduled_manager.async_session_maker", session_maker),
                patch("services.scheduled_manager.download_manager", fake_dm),
                patch("services.scheduled_manager.build_download_from_program",
                      AsyncMock(return_value=download)),
                patch.object(manager, "_get_catchup_window_days", new=archive_days),
            ):
                await manager._queue_ready_recordings()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

        self.assertEqual(
            schedule.status,
            ScheduledStatus.QUEUED.value,
            "Existing folder with space must dispatch normally (no regression).",
        )
        fake_dm.queue_download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
