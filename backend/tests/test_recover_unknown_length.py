import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus


def _make_recovery_session(downloads, settings=None):
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


class RecoverUnknownLengthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_length_partial_file_preserves_bytes_on_recovery(self):
        """
        BUG: recover_incomplete_downloads resets downloaded_bytes=0 for every
        incomplete DOWNLOADING record, including streams where the provider sent no
        Content-Length (file_size=0).

        Root cause: `and download.file_size` is falsy when file_size=0, so
        is_complete is always False for unknown-length downloads. The code then
        unconditionally resets downloaded_bytes=0, discarding partial progress.

        For a near-the-window-edge recording this means: restart -> full re-download
        required -> catchup window closes before re-download finishes -> recording lost.

        Fix: when file_size==0 and a non-empty partial file exists on disk, set
        downloaded_bytes to the actual on-disk size instead of resetting to 0.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x00" * 4096)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 42
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 0
            download.downloaded_bytes = 4096
            download.error_message = None

            manager = DownloadManager()

            with (
                patch("services.download_manager.async_session_maker", _make_recovery_session([download])),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
                patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed_test"),
                patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
            ):
                await manager.recover_incomplete_downloads()

            # The partial file has 4096 bytes on disk.
            # After recovery the DB must reflect that, not 0.
            self.assertEqual(
                download.downloaded_bytes,
                4096,
                "downloaded_bytes must reflect the on-disk partial file size when "
                "file_size=0 (unknown-length stream). Resetting to 0 loses progress "
                "and forces a full re-download that may exceed the catchup window.",
            )
            self.assertEqual(
                download.status,
                DownloadStatus.PENDING.value,
                "Unknown-length partial download must be requeued as PENDING.",
            )
        finally:
            os.unlink(tmp_path)

    async def test_known_length_incomplete_still_resets_bytes(self):
        """
        When file_size is known (>0) but the download is incomplete, downloaded_bytes
        should still be reset to 0. No regression from the fix.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x00" * 512)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 43
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 10000
            download.downloaded_bytes = 512
            download.error_message = None

            manager = DownloadManager()

            with (
                patch("services.download_manager.async_session_maker", _make_recovery_session([download])),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
                patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed_test"),
                patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
            ):
                await manager.recover_incomplete_downloads()

            self.assertEqual(
                download.downloaded_bytes,
                0,
                "Known-length incomplete download must reset downloaded_bytes to 0.",
            )
        finally:
            os.unlink(tmp_path)

    async def test_unknown_length_missing_file_resets_bytes(self):
        """
        When file_size=0 but no partial file exists on disk, downloaded_bytes resets
        to 0 (nothing to preserve).
        """
        download = MagicMock()
        download.id = 44
        download.status = DownloadStatus.DOWNLOADING.value
        download.output_path = "/tmp/nonexistent_recovery_stream_test.ts"
        download.file_size = 0
        download.downloaded_bytes = 1024
        download.error_message = None

        manager = DownloadManager()

        with (
            patch("services.download_manager.async_session_maker", _make_recovery_session([download])),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
            patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed_test"),
            patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
        ):
            await manager.recover_incomplete_downloads()

        self.assertEqual(
            download.downloaded_bytes,
            0,
            "No partial file on disk: downloaded_bytes must reset to 0.",
        )


if __name__ == "__main__":
    unittest.main()
