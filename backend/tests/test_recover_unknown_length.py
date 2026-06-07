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
        recover_incomplete_downloads must not reset downloaded_bytes=0 for a partial
        chunked (unknown Content-Length) download when a partial file exists on disk.

        Scenario: 4096 bytes written to disk; the DB had recorded 8192 bytes before the
        crash (the download was still in progress). The DB value is stale; the on-disk
        size is the truth. After recovery downloaded_bytes must equal the on-disk size
        (4096), not 0 (the old buggy reset) and not 8192 (the stale DB value).

        disk_size < downloaded_bytes → partial, not complete → re-queue as PENDING.
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
            download.downloaded_bytes = 8192  # stale DB value, more than what's on disk
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

            # downloaded_bytes must reflect the on-disk size (4096), not 0 or the stale 8192.
            self.assertEqual(
                download.downloaded_bytes,
                4096,
                "downloaded_bytes must be set to on-disk size when file_size=0 and "
                "disk_size < downloaded_bytes (partial stream). Resetting to 0 loses "
                "progress; keeping the stale DB value would send a bad Range offset.",
            )
            self.assertEqual(
                download.status,
                DownloadStatus.PENDING.value,
                "Partial chunked download (disk_size < downloaded_bytes) must be requeued as PENDING.",
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
