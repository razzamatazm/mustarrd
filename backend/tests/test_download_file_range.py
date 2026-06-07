"""
Tests for HTTP Range resume support in _download_file and the offset propagation
from _execute_download.

Root cause addressed: when an unknown-length stream was recovered after a restart,
recover_incomplete_downloads correctly set downloaded_bytes to the on-disk size,
but _execute_download immediately reset it to 0 and _download_file opened the file
with 'wb' (truncating the partial data).  The fix:
  - _execute_download saves downloaded_bytes as recovery_offset before resetting.
  - _download_file accepts an offset parameter; if offset > 0 it sends a
    Range: bytes=<offset>- header.
  - 206 response: file opened 'ab', downloaded counter starts at offset (resume).
  - 200 response: file opened 'wb', downloaded counter starts at 0 (restart).
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_aiohttp_response(status, chunks, content_range=None):
    """Return a mock aiohttp response that yields *chunks* from iter_chunked."""
    response = MagicMock()
    response.status = status
    response.content_length = None
    response.reason = "OK"

    # Support both dict-style and .get() header access used in _download_file
    headers = {}
    if content_range:
        headers["Content-Range"] = content_range
    response.headers = headers

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


def _make_aiohttp_client_session(response):
    """Return (mock_session, patch_target) that yields *response* from .get()."""
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


# ---------------------------------------------------------------------------
# _execute_download offset propagation
# ---------------------------------------------------------------------------

class ExecuteDownloadOffsetTests(unittest.TestCase):
    """_execute_download must forward downloaded_bytes as offset to _download_file."""

    def setUp(self):
        self.manager = DownloadManager()

    def test_execute_download_passes_recovered_bytes_as_offset(self):
        """
        When a recovered unknown-length download has downloaded_bytes=4096 in the DB,
        _execute_download must call _download_file with offset=4096 so the provider
        can resume the transfer instead of re-downloading from scratch.
        """
        captured_offset = []

        async def capture_offset(url, path, dl_id, ses, offset=0):
            captured_offset.append(offset)
            return 1024

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_file
            mock_download.source_url = "http://provider/stream"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 4096  # set by recover_incomplete_downloads
            mock_download.file_size = 0
            mock_download.error_message = None

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_download
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.commit = AsyncMock()

            @asynccontextmanager
            async def mock_session_maker():
                yield mock_session

            with (
                patch("services.download_manager.async_session_maker", mock_session_maker),
                patch.object(self.manager, "_download_file", capture_offset),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_needs_post_processing", return_value=False),
                patch.object(self.manager, "_move_to_completed", return_value=output_file),
            ):
                asyncio.run(self.manager._execute_download(1))

        self.assertEqual(
            captured_offset,
            [4096],
            "_execute_download must pass downloaded_bytes as offset to _download_file. "
            "Without this, _download_file cannot send a Range header and the provider "
            "re-streams from the beginning, risking catchup-window expiry.",
        )

    def test_execute_download_passes_zero_offset_for_normal_download(self):
        """A normal (non-recovered) download has downloaded_bytes=0 and must pass offset=0."""
        captured_offset = []

        async def capture_offset(url, path, dl_id, ses, offset=0):
            captured_offset.append(offset)
            return 512

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 2
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_file
            mock_download.source_url = "http://provider/stream"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 0
            mock_download.file_size = 0
            mock_download.error_message = None

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_download
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.commit = AsyncMock()

            @asynccontextmanager
            async def mock_session_maker():
                yield mock_session

            with (
                patch("services.download_manager.async_session_maker", mock_session_maker),
                patch.object(self.manager, "_download_file", capture_offset),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_needs_post_processing", return_value=False),
                patch.object(self.manager, "_move_to_completed", return_value=output_file),
            ):
                asyncio.run(self.manager._execute_download(2))

        self.assertEqual(captured_offset, [0])


# ---------------------------------------------------------------------------
# _download_file Range behaviour
# ---------------------------------------------------------------------------

class DownloadFileRangeTests(unittest.IsolatedAsyncioTestCase):
    """_download_file Range header and file-mode selection."""

    async def test_range_header_sent_when_offset_nonzero(self):
        """offset>0 must produce a Range: bytes=<offset>- request header."""
        new_data = b"\x47" * 512
        response = _make_aiohttp_response(206, [new_data], content_range="bytes 4096-4607/4608")
        mock_session, client_cm = _make_aiohttp_client_session(response)

        manager = DownloadManager()
        db_session = _make_db_session()

        tmp_f = tempfile.NamedTemporaryFile(suffix=".ts", delete=False)
        tmp_f.write(b"\x00" * 4096)
        tmp_f.close()
        tmp_path = tmp_f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=4096
                )

            mock_session.get.assert_called_once()
            _url, _kwargs = mock_session.get.call_args[0], mock_session.get.call_args[1]
            sent_headers = _kwargs.get("headers", {})
            self.assertEqual(
                sent_headers.get("Range"),
                "bytes=4096-",
                "Range header must be sent when offset>0.",
            )
        finally:
            os.unlink(tmp_path)

    async def test_206_response_appends_and_counts_from_offset(self):
        """
        A 206 response means the server honours the Range request.
        _download_file must open the file in 'ab' mode and return offset + new bytes.
        """
        existing = b"\x00" * 4096
        new_data = b"\x47" * 512
        response = _make_aiohttp_response(206, [new_data], content_range="bytes 4096-4607/4608")
        _mock_session, client_cm = _make_aiohttp_client_session(response)

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
                    "http://provider/stream", tmp_path, 1, db_session, offset=4096
                )

            # Return value must be offset + newly received bytes (cumulative)
            self.assertEqual(
                total,
                4096 + 512,
                "_download_file must return offset+new_bytes on 206 (cumulative count).",
            )
            # File must contain old bytes plus new bytes (append, not overwrite)
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(len(contents), 4096 + 512, "File must be appended, not truncated.")
            self.assertEqual(contents[:4096], existing)
            self.assertEqual(contents[4096:], new_data)
        finally:
            os.unlink(tmp_path)

    async def test_200_response_restarts_when_range_not_honoured(self):
        """
        A 200 response when offset>0 means the server ignored the Range header.
        _download_file must fall back to 'wb' mode and return only new bytes (no offset).
        """
        existing = b"\x00" * 4096
        new_data = b"\x47" * 512
        response = _make_aiohttp_response(200, [new_data])
        _mock_session, client_cm = _make_aiohttp_client_session(response)

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
                    "http://provider/stream", tmp_path, 1, db_session, offset=4096
                )

            # Return value must be only the newly received bytes (restart from 0)
            self.assertEqual(
                total,
                512,
                "_download_file must return only new bytes on 200 (no offset added).",
            )
            # File must be overwritten (truncated), not appended
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(contents, new_data, "File must be overwritten on 200 fallback.")
        finally:
            os.unlink(tmp_path)

    async def test_206_content_range_start_mismatch_restarts(self):
        """
        A 206 response where Content-Range start != requested offset means the provider
        returned data from the wrong position. _download_file must fall back to 'wb'
        mode and restart from 0 rather than appending corrupted data.
        """
        existing = b"\x00" * 4096
        # Provider returns 206 but Content-Range says it started at byte 0, not 4096
        new_data = b"\x47" * 512
        response = _make_aiohttp_response(206, [new_data], content_range="bytes 0-511/10000")
        _mock_session, client_cm = _make_aiohttp_client_session(response)

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
                    "http://provider/stream", tmp_path, 1, db_session, offset=4096
                )

            # Must restart: return value is only new bytes (no offset added)
            self.assertEqual(
                total,
                512,
                "Content-Range start mismatch must trigger restart; return value must not include offset.",
            )
            # File must be overwritten (truncated), not appended
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(contents, new_data, "File must be overwritten when Content-Range start != offset.")
        finally:
            os.unlink(tmp_path)

    async def test_no_range_header_when_offset_zero(self):
        """offset=0 (normal download) must not send a Range header."""
        new_data = b"\x47" * 256
        response = _make_aiohttp_response(200, [new_data])
        mock_session, client_cm = _make_aiohttp_client_session(response)

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=0
                )

            _url, _kwargs = mock_session.get.call_args[0], mock_session.get.call_args[1]
            sent_headers = _kwargs.get("headers", {})
            self.assertNotIn(
                "Range",
                sent_headers,
                "No Range header must be sent when offset is 0.",
            )
        finally:
            os.unlink(tmp_path)


    @unittest.expectedFailure
    async def test_206_no_content_range_falls_back_to_restart(self):
        """
        BUG: A 206 response with no Content-Range header causes false resume.

        Root cause: when the server returns 206 but omits the Content-Range header,
        range_start stays None.  The mismatch guard is:
            range_mismatch = range_start is not None and range_start != offset
        Since range_start is None, range_mismatch is False, so resuming evaluates to
        True.  The file is opened in 'ab' mode even though we have no confirmation
        that the server is sending from the right position.  A buggy or proxied IPTV
        provider that returns 206 from byte 0 (without Content-Range) would cause the
        partial file to grow from offset+0 to offset+server_full_size, producing a
        corrupt recording.

        Expected fix: treat absent Content-Range as "position unknown" and fall back
        to restart ('wb', downloaded=0), the same as a Content-Range mismatch.
        Guard should be:
            resuming = status==206 and offset>0 and range_start is not None
                       and range_start == offset
        """
        existing = b"\x00" * 4096
        # Provider returns 206 but NO Content-Range header (RFC violation, common in
        # cheap IPTV catchup implementations and some transparent proxies).
        new_data = b"\x47" * 512
        response = _make_aiohttp_response(206, [new_data])  # content_range=None
        _mock_session, client_cm = _make_aiohttp_client_session(response)

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
                    "http://provider/stream", tmp_path, 1, db_session, offset=4096
                )

            # Must fall back to restart: file overwritten, return value is only new bytes.
            self.assertEqual(
                total,
                512,
                "206 with no Content-Range must restart (return new bytes only, not offset+new). "
                "Currently returns 4096+512 because range_start=None bypasses the mismatch guard, "
                "treating absent Content-Range as a confirmed resume.",
            )
            with open(tmp_path, "rb") as fh:
                contents = fh.read()
            self.assertEqual(
                contents,
                new_data,
                "File must be overwritten when Content-Range is absent, not appended. "
                "Currently the partial file is opened 'ab' and the server's bytes "
                "(from an unknown position) are appended, corrupting the recording.",
            )
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
