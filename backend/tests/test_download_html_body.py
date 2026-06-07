"""
Regression test: provider returns HTTP 200 with a text/html body (error page).

Before the fix, _download_file would write the HTML to the output file and mark
the download COMPLETED. With post-processing off the user ended up with an HTML
file that looked like a completed recording. With post-processing on, FFmpeg
failed on the HTML input and the download ended as COMPLETED with a warning.

After the fix, _download_file raises an exception as soon as it sees a text/*
Content-Type so the download is marked FAILED with a clear message.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


def _make_response(status, content_type, chunks):
    response = MagicMock()
    response.status = status
    response.reason = "OK"
    response.content_length = None
    response.headers = {"Content-Type": content_type}

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


def _make_client_session(response):
    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=response)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = get_cm

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=mock_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)

    return client_cm


class HtmlBodyRejectionTests(unittest.IsolatedAsyncioTestCase):

    async def test_html_200_raises_exception(self):
        """HTTP 200 with text/html body must raise, not write HTML to disk."""
        html = b"<html><body>Catchup not available</body></html>"
        response = _make_response(200, "text/html; charset=utf-8", [html])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    with self.assertRaises(Exception) as ctx:
                        await manager._download_file(
                            "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts",
                            tmp_path,
                            1,
                            db_session,
                            offset=0,
                        )

        self.assertIn(
            "error page",
            str(ctx.exception).lower(),
            "Exception message must mention 'error page' so the failure reason "
            "is understandable in the download log.",
        )

    async def test_html_206_raises_exception(self):
        """HTTP 206 with text/html body must also raise."""
        html = b"<html>Session limit reached</html>"
        response = _make_response(206, "text/html", [html])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    with self.assertRaises(Exception) as ctx:
                        await manager._download_file(
                            "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts",
                            tmp_path,
                            1,
                            db_session,
                            offset=0,
                        )

        self.assertIn("error page", str(ctx.exception).lower())

    async def test_text_plain_raises_exception(self):
        """text/plain is also a non-video response and must be rejected."""
        response = _make_response(200, "text/plain", [b"Catchup unavailable"])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    with self.assertRaises(Exception):
                        await manager._download_file(
                            "http://provider/stream",
                            tmp_path,
                            1,
                            db_session,
                            offset=0,
                        )

    async def test_video_mpeg_not_rejected(self):
        """video/mpeg must not be rejected. Normal IPTV streams must still work."""
        data = b"\x47" * 1024
        response = _make_response(200, "video/mpeg", [data])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    total = await manager._download_file(
                        "http://provider/stream",
                        tmp_path,
                        1,
                        db_session,
                        offset=0,
                    )

        self.assertEqual(total, 1024, "video/mpeg response must complete normally.")

    async def test_octet_stream_not_rejected(self):
        """application/octet-stream must not be rejected."""
        data = b"\x47" * 512
        response = _make_response(200, "application/octet-stream", [data])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    total = await manager._download_file(
                        "http://provider/stream",
                        tmp_path,
                        1,
                        db_session,
                        offset=0,
                    )

        self.assertEqual(total, 512, "application/octet-stream must complete normally.")

    async def test_no_content_type_not_rejected(self):
        """Absent Content-Type header must not be rejected (many providers omit it)."""
        data = b"\x47" * 256
        response = _make_response(200, "", [data])
        client_cm = _make_client_session(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        with patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm):
            with patch.object(manager, "_broadcast_log", AsyncMock()):
                with patch.object(manager, "_broadcast_progress", AsyncMock()):
                    total = await manager._download_file(
                        "http://provider/stream",
                        tmp_path,
                        1,
                        db_session,
                        offset=0,
                    )

        self.assertEqual(total, 256, "Missing Content-Type must not block the download.")


if __name__ == "__main__":
    unittest.main()
