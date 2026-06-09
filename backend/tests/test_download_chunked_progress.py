"""
Regression: chunked-transfer downloads (no Content-Length) never updated DB
progress or broadcast to the UI during the transfer.

Root cause: the emit condition in _stream_response_to_file was
``progress - last_progress_update >= 1 or downloaded == total_size``.
For chunked streams progress stays 0 and total_size is 0, so neither side
ever became true while bytes were flowing: the Downloads page showed a frozen
0% entry for the whole transfer and the DB byte count stayed stale.

Fix: for total_size == 0 the loop now persists downloaded_bytes and broadcasts
(indeterminate, with the running byte count) every
CHUNKED_PROGRESS_INTERVAL_BYTES. The final post-stream commit now also sets
progress=100 as a completion marker, and recover_incomplete_downloads only
treats a chunked file as complete when that marker is present — so a crash
right after a periodic mid-stream commit (disk size == committed bytes,
progress still 0) is requeued instead of being finalized as a truncated
"complete" recording.
"""

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


def _make_chunked_response(chunks):
    response = MagicMock()
    response.status = 200
    response.reason = "OK"
    response.headers = {}
    response.content_length = None

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


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


class ChunkedProgressEmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_stream_broadcasts_and_commits_periodically(self):
        """
        Four 1 KiB chunks with the emit interval patched to 1 KiB must produce
        a broadcast and a DB commit at every interval boundary, each carrying
        the running byte count.
        """
        chunks = [b"\x47" * 1024] * 4
        response = _make_chunked_response(chunks)

        executed = []

        async def capture_execute(stmt):
            executed.append(stmt)
            return MagicMock()

        db_session = AsyncMock()
        db_session.execute = capture_execute
        db_session.commit = AsyncMock()

        manager = DownloadManager()
        broadcast = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.CHUNKED_PROGRESS_INTERVAL_BYTES", 1024),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", broadcast),
            ):
                total = await manager._stream_response_to_file(
                    response=response,
                    output_path=tmp_path,
                    file_mode="wb",
                    downloaded_start=0,
                    total_size=0,
                    download_id=21,
                    session=db_session,
                )
        finally:
            os.unlink(tmp_path)

        self.assertEqual(total, 4096)
        self.assertEqual(
            broadcast.call_count, 4,
            "Chunked transfer must broadcast progress at every interval boundary. "
            "Zero broadcasts means the UI shows a frozen download.",
        )
        reported_bytes = [
            call.kwargs.get("downloaded_bytes") for call in broadcast.call_args_list
        ]
        self.assertEqual(
            reported_bytes, [1024, 2048, 3072, 4096],
            "Each broadcast must carry the running byte count.",
        )
        for call in broadcast.call_args_list:
            self.assertEqual(call.args[2], DownloadStatus.DOWNLOADING.value)
            self.assertTrue(
                call.kwargs.get("indeterminate"),
                "Chunked progress is indeterminate (no Content-Length).",
            )
        self.assertEqual(
            len(executed), 5,
            "Four periodic downloaded_bytes commits plus the final completion commit.",
        )

    async def test_chunked_final_commit_marks_progress_complete(self):
        """
        The post-stream commit must set progress=100 along with downloaded_bytes;
        recovery uses it to distinguish a finished chunked stream from a crash
        right after a periodic mid-stream commit.
        """
        response = _make_chunked_response([b"\x47" * 512])

        executed = []

        async def capture_execute(stmt):
            executed.append(stmt)
            return MagicMock()

        db_session = AsyncMock()
        db_session.execute = capture_execute
        db_session.commit = AsyncMock()

        manager = DownloadManager()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                await manager._stream_response_to_file(
                    response=response,
                    output_path=tmp_path,
                    file_mode="wb",
                    downloaded_start=0,
                    total_size=0,
                    download_id=22,
                    session=db_session,
                )
        finally:
            os.unlink(tmp_path)

        self.assertEqual(len(executed), 1, "512 B is below the interval: only the final commit.")
        sql = str(executed[0].compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("downloaded_bytes", sql)
        self.assertIn("progress", sql)
        self.assertIn("100.0", sql, "Final commit must mark progress=100 (completion marker).")


class ChunkedRecoveryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_mid_stream_commit_state_is_requeued_not_finalized(self):
        """
        Crash right after a periodic mid-stream commit: DB downloaded_bytes
        equals the on-disk size but progress is still 0 (no completion marker).
        Recovery must requeue as PENDING, not finalize a truncated recording.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x47" * 8192)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 60
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 0
            download.downloaded_bytes = 8192  # periodic commit matched disk at crash
            download.progress = 0.0  # completion marker absent: stream was mid-flight
            download.error_message = None
            download.is_vod = False

            manager = DownloadManager()

            with (
                patch("services.download_manager.async_session_maker", _make_recovery_session([download])),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
                patch.object(manager, "_resolve_completed_folder", return_value="/tmp/completed_test"),
                patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
                patch.object(manager, "_sync_schedule_status", AsyncMock()),
            ):
                await manager.recover_incomplete_downloads()

            self.assertEqual(
                download.status,
                DownloadStatus.PENDING.value,
                "Mid-stream chunked state (progress=0) must be requeued, not "
                "finalized as a truncated 'complete' recording.",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def test_completed_chunked_state_is_still_finalized(self):
        """
        The genuine post-stream state (progress=100, bytes matching disk)
        must still be finalized as COMPLETED.
        """
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x47" * 8192)
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 61
            download.status = DownloadStatus.DOWNLOADING.value
            download.output_path = tmp_path
            download.file_size = 0
            download.downloaded_bytes = 8192
            download.progress = 100.0  # completion marker from the final commit
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

            self.assertEqual(
                download.status,
                DownloadStatus.COMPLETED.value,
                "Complete chunked download must be finalized on recovery.",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
