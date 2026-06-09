"""
Regression: a complete recording on disk was fully re-downloaded on restart
when the provider ignored the Range request.

Scenario: a chunked recording finishes streaming but the app crashes before
the COMPLETED commit. recover_incomplete_downloads requeues it with
downloaded_bytes set to the on-disk size, so _download_file sends
Range: bytes=<size>-. Providers that honour Range return 416 (handled:
finalized). Providers that ignore Range return 200 with the full body and a
Content-Length.

Root cause: the 200-with-offset branch in _download_file_once always fell back
to a 'wb' re-download from byte 0, even when the response's Content-Length
showed the file on disk was already complete.

Fix: when the provider ignores Range (200, offset>0) and Content-Length is
known, compare it against the on-disk size; if the file already covers the
full content length, keep it and return its size instead of re-streaming.
"""

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


def _make_response(status, chunks, content_length=None):
    response = MagicMock()
    response.status = status
    response.content_length = content_length
    response.reason = "OK"
    response.headers = {}

    chunks_consumed = []

    async def iter_chunked(_size):
        for chunk in chunks:
            chunks_consumed.append(chunk)
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response, chunks_consumed


def _make_client_session(response):
    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=response)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = get_cm

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=mock_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_session, client_cm


def _make_db_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


class RangeIgnoredCompleteFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_200_with_content_length_covered_by_disk_keeps_file(self):
        """
        offset == on-disk size == Content-Length of the 200 response:
        the recording is already complete. The file must not be re-downloaded
        or truncated; _download_file returns the on-disk size.
        """
        size = 8192
        content = b"\x47" * size
        # Provider ignores Range, returns 200 with the full body.
        response, consumed = _make_response(200, [content], content_length=size)
        mock_session, client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=size
                )

            self.assertEqual(
                total, size,
                "Complete file on disk must be kept (return its size), not re-downloaded.",
            )
            self.assertEqual(
                consumed, [],
                "Response body must not be streamed when the file is already complete.",
            )
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), content, "File on disk must be untouched.")
        finally:
            os.unlink(tmp_path)

    async def test_200_with_content_length_larger_than_disk_restarts(self):
        """
        Content-Length larger than the on-disk size: the file is a genuine
        partial and the provider cannot resume, so re-download from byte 0.
        """
        partial = b"\x00" * 1024
        full_content = b"\x47" * 4096
        response, _consumed = _make_response(200, [full_content], content_length=4096)
        mock_session, client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(partial)
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=1024
                )

            self.assertEqual(total, 4096, "Partial file must be re-downloaded in full.")
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), full_content, "File must be the full restarted body.")
        finally:
            os.unlink(tmp_path)

    async def test_200_without_content_length_still_restarts(self):
        """
        No Content-Length on the 200 response: completeness cannot be verified,
        so the existing restart-from-scratch behaviour is kept.
        """
        full_content = b"\x47" * 2048
        response, _consumed = _make_response(200, [full_content], content_length=None)
        mock_session, client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x00" * 2048)
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=2048
                )

            self.assertEqual(total, 2048)
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), full_content)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
