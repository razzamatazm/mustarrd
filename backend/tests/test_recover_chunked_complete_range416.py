"""
BUG: Chunked (no Content-Length) download that completes all bytes but crashes
before the status-transition commit is re-queued as PENDING on the next startup.

recovery_offset is then set to the full on-disk file size, so _download_file
sends Range: bytes={full_size}-. Most IPTV providers return HTTP 416 Range Not
Satisfiable; the exception handler marks the download FAILED and calls os.unlink
on the output file. A finished recording is permanently destroyed.

Root cause: is_complete check in recover_incomplete_downloads requires
download.file_size to be truthy. Chunked-encoding streams have file_size=0
throughout, so the expression always short-circuits to False and a complete
chunked file is indistinguishable from a partial one.

Fix needed: when file_size==0 and the on-disk file is non-empty and
downloaded_bytes >= on-disk size, treat the download as complete and advance
it to PROCESSING or COMPLETED instead of re-queuing.
"""

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


class RecoverChunkedCompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_complete_file_not_requeued_for_redownload(self):
        """
        A chunked download whose on-disk bytes match downloaded_bytes should be
        detected as complete on recovery, not re-queued as PENDING.

        Current behavior (bug): status set to PENDING; recovery_offset = full
        file size; next _download_file call sends Range: bytes={full_size}-;
        provider returns 416; exception handler deletes the completed file.

        Expected: status advanced to COMPLETED (or PROCESSING if post-processing
        is configured), not re-queued.

        This test FAILS with the current code and should PASS after the fix.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x47" * 8192)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 55
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 0          # chunked stream: no Content-Length
            download.downloaded_bytes = 8192  # DB matches disk: download was complete
            download.error_message = None
            download.is_vod = False

            manager = DownloadManager()

            with (
                patch("services.download_manager.async_session_maker", _make_recovery_session([download])),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
                patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed_test"),
                patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
                patch.object(manager, "_move_to_completed", return_value="/tmp/completed_test/test.ts"),
                patch.object(manager, "_sync_schedule_status", AsyncMock()),
            ):
                await manager.recover_incomplete_downloads()

            # BUG: current code sets status to PENDING, meaning recovery_offset
            # will be 8192 on next attempt and Range: bytes=8192- risks HTTP 416
            # which deletes the completed file.
            self.assertNotEqual(
                download.status,
                DownloadStatus.PENDING.value,
                "Chunked download with matching on-disk bytes wrongly re-queued as "
                "PENDING. On next attempt Range: bytes=8192- will likely return "
                "HTTP 416 and unlink the completed recording.",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def test_chunked_partial_file_still_requeued(self):
        """
        A chunked download whose on-disk bytes are smaller than downloaded_bytes
        (genuine partial: crash mid-stream) should still be re-queued as PENDING
        so the download retries from where it left off.

        This is the non-regressed case: partial files must not be falsely marked
        complete.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x47" * 1024)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 56
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 0     # chunked stream
            download.downloaded_bytes = 4096  # DB says 4096 but disk has only 1024
            download.error_message = None
            download.is_vod = False

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
                download.status,
                DownloadStatus.PENDING.value,
                "Partial chunked download must be re-queued as PENDING.",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
