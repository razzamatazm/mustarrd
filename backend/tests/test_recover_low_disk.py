"""
Regression: recover_incomplete_downloads re-queues PENDING downloads without a
disk-space check. If the disk filled while the server was down, those downloads
restart and hit ENOSPC with a cryptic OS error.

After fix: if free space is below min_free_space_gb at startup, PENDING
downloads (including those reset from DOWNLOADING) are marked FAILED with a
plain "Disk space below minimum at startup" message instead of being queued.
"""
import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus


def _make_recovery_session(downloads, min_free_gb=25):
    settings = MagicMock()
    settings.download_folder = "/tmp/dl"
    settings.min_free_space_gb = min_free_gb
    settings.max_concurrent_downloads = 2
    settings.max_concurrent_post_processing = 2
    settings.transcode_enabled = False
    settings.comskip_enabled = False

    settings_result = MagicMock()
    settings_result.scalar_one_or_none.return_value = settings

    downloads_result = MagicMock()
    downloads_result.scalars.return_value.all.return_value = downloads

    call_count = [0]

    async def execute(stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return settings_result
        return downloads_result

    session = AsyncMock()
    session.execute = execute
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


def _pending_download(dl_id=1, output_path="/tmp/dl/show.ts"):
    dl = MagicMock()
    dl.id = dl_id
    dl.status = DownloadStatus.PENDING.value
    dl.output_path = output_path
    dl.file_size = 0
    dl.downloaded_bytes = 0
    dl.error_message = None
    dl.completed_at = None
    return dl


class RecoverLowDiskTests(unittest.IsolatedAsyncioTestCase):

    async def test_low_disk_at_startup_marks_pending_failed(self):
        """
        PENDING download must be marked FAILED when free space is below
        min_free_space_gb at restart.

        Before fix: download re-queued unconditionally; _execute_download hits
        ENOSPC and fails with a cryptic OS error.
        After fix: FAILED immediately with a plain disk-space message.
        """
        dl = _pending_download(dl_id=1)
        manager = DownloadManager()

        disk_result = MagicMock()
        disk_result.free = 5 * 1024 ** 3  # 5 GB, below 25 GB threshold

        sync_mock = AsyncMock()
        with (
            patch("services.download_manager.async_session_maker",
                  _make_recovery_session([dl], min_free_gb=25)),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_sync_schedule_status", sync_mock),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/dl"),
            patch("services.download_manager.shutil.disk_usage", return_value=disk_result),
            patch("services.download_manager.os.path.exists", return_value=True),
        ):
            count = await manager.recover_incomplete_downloads()

        self.assertEqual(
            dl.status,
            DownloadStatus.FAILED.value,
            "Low disk at startup: PENDING download must be marked FAILED, not queued.",
        )
        self.assertIsNotNone(dl.error_message)
        self.assertIn(
            "disk space",
            dl.error_message.lower(),
            "Error message must mention disk space so the operator knows why it failed.",
        )
        self.assertIsNotNone(
            dl.completed_at,
            "completed_at must be set so schedule row stays in sync.",
        )
        sync_mock.assert_called_once()
        self.assertEqual(
            count,
            0,
            "No downloads must be queued when disk is below threshold.",
        )

    async def test_sufficient_disk_queues_pending_normally(self):
        """
        When disk is above threshold, PENDING downloads are re-queued as before.
        No regression from the fix.
        """
        dl = _pending_download(dl_id=2)
        manager = DownloadManager()

        disk_result = MagicMock()
        disk_result.free = 100 * 1024 ** 3  # 100 GB free

        with (
            patch("services.download_manager.async_session_maker",
                  _make_recovery_session([dl], min_free_gb=25)),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/dl"),
            patch("services.download_manager.shutil.disk_usage", return_value=disk_result),
            patch("services.download_manager.os.path.exists", return_value=True),
        ):
            count = await manager.recover_incomplete_downloads()

        self.assertEqual(
            dl.status,
            DownloadStatus.PENDING.value,
            "Sufficient disk: PENDING download must remain PENDING and be queued.",
        )
        self.assertEqual(count, 1, "Exactly one download must be queued.")

    async def test_missing_folder_skips_disk_check(self):
        """
        If the download folder does not exist yet, skip the check and queue normally.
        Consistent with how create_download and scheduled_manager behave.
        """
        dl = _pending_download(dl_id=3)
        manager = DownloadManager()

        with (
            patch("services.download_manager.async_session_maker",
                  _make_recovery_session([dl], min_free_gb=25)),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/dl"),
            patch("services.download_manager.os.path.exists", return_value=False),
        ):
            count = await manager.recover_incomplete_downloads()

        self.assertEqual(
            dl.status,
            DownloadStatus.PENDING.value,
            "Missing folder: disk check skipped, download must be queued normally.",
        )
        self.assertEqual(count, 1)

    async def test_zero_min_free_gb_disables_check(self):
        """
        min_free_space_gb=0 disables the guard so the download is queued regardless.
        """
        dl = _pending_download(dl_id=4)
        manager = DownloadManager()

        disk_result = MagicMock()
        disk_result.free = 0  # effectively empty disk

        with (
            patch("services.download_manager.async_session_maker",
                  _make_recovery_session([dl], min_free_gb=0)),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/dl"),
            patch("services.download_manager.shutil.disk_usage", return_value=disk_result),
            patch("services.download_manager.os.path.exists", return_value=True),
        ):
            count = await manager.recover_incomplete_downloads()

        self.assertEqual(
            dl.status,
            DownloadStatus.PENDING.value,
            "min_free_space_gb=0 disables the guard; download must be queued.",
        )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
