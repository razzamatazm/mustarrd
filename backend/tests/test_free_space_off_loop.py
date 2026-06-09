"""
Regression tests: free-space checks must not block the event loop.

shutil.disk_usage (and os.path.exists) on a slow or hung network mount can
stall for minutes. The scheduler poll, download recovery, and the per-download
Content-Length preflight all ran it synchronously on the event loop, freezing
every download and WebSocket update.

Fix: the stats run in a worker thread (asyncio.to_thread); the shared probe in
services.disk_space additionally caps the wait with a timeout and reports the
check as unavailable instead of hanging.
"""
import sys
import threading
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.disk_space import get_free_space_gb
from services.download_manager import DownloadManager


def _usage(free_bytes):
    usage = MagicMock()
    usage.free = free_bytes
    return usage


class GetFreeSpaceOffLoopTests(unittest.IsolatedAsyncioTestCase):

    async def test_disk_usage_runs_off_the_event_loop_thread(self):
        loop_thread = threading.current_thread()
        seen_threads = []

        def recording_disk_usage(path):
            seen_threads.append(threading.current_thread())
            return _usage(100 * 1024 ** 3)

        with (
            patch("services.disk_space.os.path.exists", return_value=True),
            patch("services.disk_space.shutil.disk_usage", recording_disk_usage),
        ):
            result = await get_free_space_gb("/mnt/nas")

        self.assertEqual(result, 100.0)
        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(
            seen_threads[0],
            loop_thread,
            "shutil.disk_usage ran on the event-loop thread; a hung NAS mount "
            "would freeze the whole application.",
        )

    async def test_hung_mount_times_out_and_reports_unavailable(self):
        def hung_disk_usage(path):
            time.sleep(0.5)  # simulates a hung network mount
            return _usage(100 * 1024 ** 3)

        with (
            patch("services.disk_space.os.path.exists", return_value=True),
            patch("services.disk_space.shutil.disk_usage", hung_disk_usage),
        ):
            started = time.monotonic()
            result = await get_free_space_gb("/mnt/nas", timeout=0.05)
            elapsed = time.monotonic() - started

        self.assertIsNone(result, "A hung mount must report unavailable, not hang.")
        self.assertLess(
            elapsed,
            0.45,
            "get_free_space_gb waited for the hung stat instead of timing out.",
        )


class PreflightDiskSpaceOffLoopTests(unittest.IsolatedAsyncioTestCase):
    """_preflight_disk_space must stat the disk in a worker thread."""

    async def test_preflight_disk_usage_runs_off_loop(self):
        manager = DownloadManager()
        loop_thread = threading.current_thread()
        seen_threads = []

        def recording_disk_usage(path):
            seen_threads.append(threading.current_thread())
            return _usage(500 * 1024 ** 3)

        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute = AsyncMock(return_value=settings_result)

        with patch("services.download_manager.shutil.disk_usage", recording_disk_usage):
            await manager._preflight_disk_space(
                session,
                "/tmp/downloads/show.ts",
                total_size=1024,
                already_have=0,
                download_id=1,
            )

        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(
            seen_threads[0],
            loop_thread,
            "Preflight disk_usage ran on the event-loop thread.",
        )


class RecoveryDiskCheckOffLoopTests(unittest.IsolatedAsyncioTestCase):
    """The startup recovery low-disk check must stat the disk in a worker thread."""

    async def test_recovery_disk_usage_runs_off_loop(self):
        from models import DownloadStatus

        loop_thread = threading.current_thread()
        seen_threads = []

        def recording_disk_usage(path):
            seen_threads.append(threading.current_thread())
            return _usage(100 * 1024 ** 3)

        dl = MagicMock()
        dl.id = 1
        dl.status = DownloadStatus.PENDING.value
        dl.output_path = "/tmp/dl/show.ts"
        dl.file_size = 0
        dl.downloaded_bytes = 0
        dl.error_message = None
        dl.completed_at = None

        settings = MagicMock()
        settings.download_folder = "/tmp/dl"
        settings.min_free_space_gb = 25
        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = settings
        downloads_result = MagicMock()
        downloads_result.scalars.return_value.all.return_value = [dl]

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            return settings_result if call_count[0] == 1 else downloads_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        @asynccontextmanager
        async def session_maker():
            yield session

        manager = DownloadManager()
        with (
            patch("services.download_manager.async_session_maker", session_maker),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/dl"),
            patch("services.download_manager.shutil.disk_usage", recording_disk_usage),
            patch("services.download_manager.os.path.exists", return_value=True),
        ):
            count = await manager.recover_incomplete_downloads()

        self.assertEqual(count, 1, "Download with plenty of space must be requeued.")
        self.assertEqual(len(seen_threads), 1)
        self.assertIsNot(
            seen_threads[0],
            loop_thread,
            "Recovery disk_usage ran on the event-loop thread.",
        )


if __name__ == "__main__":
    unittest.main()
