"""
Regression test: chunked-resume writes corrupt file when partial file is deleted
between recover_incomplete_downloads and download worker pickup.

Bug: recover_incomplete_downloads reads the on-disk partial file size and stores it
as download.downloaded_bytes. The download worker passes this as offset to
_download_file_once, which sends Range: bytes=<offset>- and receives a 206. If the
partial file was deleted (e.g. NAS flap on Unraid) between those two events,
aiofiles.open(path, "ab") creates a new empty file and writes the provider body
(bytes offset..end) starting at position 0. The result is a recording that is missing
its first <offset> bytes, silently marked COMPLETED.

Fix: before opening in "ab" mode, _download_file_once checks os.path.getsize against
offset. A mismatch triggers a full re-download from byte 0.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


def _make_response(status, chunks, content_range=None, content_type=None):
    response = MagicMock()
    response.status = status
    response.content_length = None
    response.reason = "OK"
    headers = {}
    if content_range:
        headers["Content-Range"] = content_range
    if content_type:
        headers["Content-Type"] = content_type
    response.headers = headers

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


def _make_client_session_multi(responses):
    call_index = [0]

    def get_side_effect(*args, **kwargs):
        idx = call_index[0]
        call_index[0] += 1
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=responses[idx])
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_session = MagicMock()
    mock_session.get.side_effect = get_side_effect

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=mock_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_session, client_cm


def _make_db_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


class ResumeDeletedPartialTests(unittest.IsolatedAsyncioTestCase):

    async def test_resume_falls_back_when_partial_file_missing(self):
        """
        Provider returns 206 at the requested offset, but the partial file was
        deleted between recovery and download pickup.

        Before the fix: _download_file_once opened the missing path in "ab" mode,
        created an empty file, and wrote the response body (bytes offset..end)
        starting at position 0, producing a corrupt COMPLETED recording.

        After the fix: disk size (-1) != offset triggers needs_restart; a fresh
        GET from byte 0 is issued and the full content is written correctly.
        """
        offset = 500
        full_content = b"\x47" * 1000

        # First GET: 206 from requested offset (server honoured Range)
        response_206 = _make_response(
            206, [full_content[offset:]], content_range=f"bytes {offset}-999/1000"
        )
        # Restart GET: full stream from byte 0
        response_200 = _make_response(200, [full_content])
        _mock_session, client_cm = _make_client_session_multi([response_206, response_200])

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Partial file is absent; output_path does not exist
            tmp_path = os.path.join(tmpdir, "show.ts")

            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=offset
                )

            self.assertEqual(
                total,
                len(full_content),
                "After restart, total must equal the full content length.",
            )
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(
                contents,
                full_content,
                "File must contain full content from byte 0, not a corrupt tail.",
            )

    async def test_resume_falls_back_when_partial_file_truncated(self):
        """
        Provider returns 206 at the requested offset, but the partial file is
        shorter than expected (truncated on disk, e.g. by a NAS remount).

        Disk size (100) != offset (500): triggers re-download from byte 0.
        """
        offset = 500
        full_content = b"\x47" * 1000

        response_206 = _make_response(
            206, [full_content[offset:]], content_range=f"bytes {offset}-999/1000"
        )
        response_200 = _make_response(200, [full_content])
        _mock_session, client_cm = _make_client_session_multi([response_206, response_200])

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x00" * 100)  # only 100 bytes, not the 500 expected
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=offset
                )

            self.assertEqual(total, len(full_content))
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), full_content)
        finally:
            os.unlink(tmp_path)

    async def test_resume_proceeds_when_partial_file_matches_offset(self):
        """
        Partial file is intact (disk size == offset): normal resume proceeds,
        no restart triggered.
        """
        offset = 500
        existing = b"\x00" * offset
        new_data = b"\x47" * 500

        response_206 = _make_response(
            206, [new_data], content_range=f"bytes {offset}-999/1000"
        )
        _mock_session, client_cm = _make_client_session_multi([response_206])

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(existing)
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=offset
                )

            self.assertEqual(total, offset + len(new_data))
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(contents[:offset], existing)
            self.assertEqual(contents[offset:], new_data)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
