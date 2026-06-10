"""
Regression test: provider returns HTTP 200 with an application/xml body (error).

Some Xtream Codes providers return XML error messages with HTTP 200 instead of an
HTTP error status. For example:

    HTTP/1.1 200 OK
    Content-Type: application/xml
    <error><message>Stream unavailable</message><code>403</code></error>

Before the fix, the content-type guard only rejected text/* and application/json.
application/xml slipped through both guards: the XML body was written to the output
.ts file and the download was silently marked COMPLETED with a corrupt recording.

After the fix, the guard also rejects application/xml so the download fails with a
clear error message instead.
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


class XmlBodyRejectionTests(unittest.IsolatedAsyncioTestCase):

    async def test_xml_200_raises_exception(self):
        """HTTP 200 with application/xml body must raise, not write XML to disk.

        Providers that return XML error messages with HTTP 200 and
        Content-Type: application/xml bypass both the existing text/* guard
        and the application/json guard added in PR #365. The XML body gets
        written to the output .ts file and the download is silently marked
        COMPLETED with a corrupt, non-playable recording.
        """
        xml_body = b"<error><message>Stream unavailable</message><code>403</code></error>"
        response = _make_response(200, "application/xml", [xml_body])
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
                            "http://provider/timeshift/user/pass/60/2026-06-09:00-00/1.ts",
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

    async def test_xml_200_with_charset_raises_exception(self):
        """HTTP 200 with application/xml; charset=utf-8 body must raise."""
        xml_body = b'<?xml version="1.0"?><result><status>error</status><msg>Session expired</msg></result>'
        response = _make_response(200, "application/xml; charset=utf-8", [xml_body])
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
                            "http://provider/timeshift/user/pass/60/2026-06-09:00-00/1.ts",
                            tmp_path,
                            1,
                            db_session,
                            offset=0,
                        )


if __name__ == "__main__":
    unittest.main()
