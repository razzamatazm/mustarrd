"""
Regression test: provider returns HTTP 200 with an application/json body (error).

Some Xtream Codes providers return JSON error messages with HTTP 200 instead of
an HTTP error status. For example:

    HTTP/1.1 200 OK
    Content-Type: application/json
    {"error": "Stream unavailable", "code": "403"}

Before the fix, _download_file passes the text/* guard (application/json does not
start with "text/"), writes the small JSON body to the output file, and marks the
download COMPLETED. The user ends up with a corrupt recording that Plex/Jellyfin
cannot play.

After the fix, _download_file raises as soon as it sees application/json so the
download is marked FAILED with a clear message.

This test currently FAILS: no exception is raised for application/json bodies.
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


class JsonBodyRejectionTests(unittest.IsolatedAsyncioTestCase):

    async def test_json_200_raises_exception(self):
        """HTTP 200 with application/json body must raise, not write JSON to disk.

        Providers that return JSON error messages with HTTP 200 and
        Content-Type: application/json bypass the existing text/* guard.
        The JSON body gets written to the output .ts file and the download
        is silently marked COMPLETED with a corrupt, non-playable file.
        """
        json_body = b'{"error": "Stream unavailable", "code": "403"}'
        response = _make_response(200, "application/json", [json_body])
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
            "error",
            str(ctx.exception).lower(),
            "Exception message must indicate the response was not a valid stream.",
        )

    async def test_json_200_session_limit_raises_exception(self):
        """HTTP 200 with application/json session-limit error must raise."""
        json_body = b'{"message": "maximum connections reached", "status": "error"}'
        response = _make_response(200, "application/json; charset=utf-8", [json_body])
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
                            "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts",
                            tmp_path,
                            1,
                            db_session,
                            offset=0,
                        )


if __name__ == "__main__":
    unittest.main()
