"""
Regression for: XMLTV refresh silently times out for large providers.

`get_xmltv()` was calling `session.get(url)` with no timeout override, so it used
the session default of 30 seconds. Large providers (20-200 MB XMLTV, slow links) hit
this limit and silently left the EPG empty. The fix passes XMLTV_TIMEOUT (300 s) so
large feeds have time to complete.

VOD calls already had a dedicated 90-second override (VOD_TIMEOUT). This test asserts
the same pattern is now applied to get_xmltv().
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import aiohttp
from services.xtream_client import XtreamClient, XMLTV_TIMEOUT


def _mock_session_returning_bytes(body: bytes, status: int = 200):
    response = MagicMock()
    response.status = status
    response.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return AsyncMock(return_value=session), session


def _client():
    return XtreamClient("http://provider.test:8080", "user", "pass")


class XmltVTimeoutTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_xmltv_passes_xmltv_timeout(self):
        """get_xmltv must pass XMLTV_TIMEOUT to session.get, not the 30s session default."""
        client = _client()
        get_session, session = _mock_session_returning_bytes(b"<tv></tv>")
        client._get_session = get_session
        await client.get_xmltv()
        _, kwargs = session.get.call_args
        timeout = kwargs.get("timeout")
        self.assertIsNotNone(timeout, "get_xmltv called session.get without a timeout kwarg")
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertEqual(
            timeout.total,
            XMLTV_TIMEOUT.total,
            f"get_xmltv uses {timeout.total}s timeout, expected {XMLTV_TIMEOUT.total}s",
        )

    async def test_xmltv_timeout_is_longer_than_session_default(self):
        """XMLTV_TIMEOUT must exceed the session default (30 s) by a meaningful margin."""
        self.assertGreater(
            XMLTV_TIMEOUT.total,
            30,
            "XMLTV_TIMEOUT must be longer than the 30s session default",
        )

    async def test_get_xmltv_returns_body(self):
        """get_xmltv returns the raw bytes on HTTP 200."""
        client = _client()
        body = b"<tv><channel id='1'></channel></tv>"
        get_session, _ = _mock_session_returning_bytes(body)
        client._get_session = get_session
        result = await client.get_xmltv()
        self.assertEqual(result, body)

    async def test_get_xmltv_raises_on_non_200(self):
        """get_xmltv raises on non-200 HTTP status."""
        client = _client()
        get_session, _ = _mock_session_returning_bytes(b"", status=503)
        client._get_session = get_session
        with self.assertRaises(Exception) as ctx:
            await client.get_xmltv()
        self.assertIn("503", str(ctx.exception))

    async def test_xmltv_timeout_constant_is_300s(self):
        """XMLTV_TIMEOUT is 300 seconds as specified."""
        self.assertEqual(XMLTV_TIMEOUT.total, 300)


if __name__ == "__main__":
    unittest.main()
