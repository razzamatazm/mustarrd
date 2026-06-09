"""
Regression for: channel list and EPG backfill requests time out on large providers.

`get_live_streams()` and `get_epg()` called `session.get(url)` with no timeout
override, so they used the session default of 30 seconds. Large providers
(tens of thousands of channels, months of per-channel EPG) routinely exceed
this, while VOD calls already had a 90-second override (VOD_TIMEOUT). The fix
gives the heavy list/EPG endpoints the same longer timeout via LIST_TIMEOUT.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import aiohttp
from services.xtream_client import XtreamClient, LIST_TIMEOUT, VOD_TIMEOUT


def _mock_session_returning_json(payload, status: int = 200):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return AsyncMock(return_value=session), session


def _client():
    return XtreamClient("http://provider.test:8080", "user", "pass")


def _timeout_passed_to(session):
    _, kwargs = session.get.call_args
    return kwargs.get("timeout")


class ListTimeoutTests(unittest.IsolatedAsyncioTestCase):

    async def test_get_live_streams_uses_list_timeout(self):
        """get_live_streams must pass LIST_TIMEOUT, not the 30s session default."""
        client = _client()
        get_session, session = _mock_session_returning_json([{"stream_id": 1}])
        client._get_session = get_session
        result = await client.get_live_streams()
        self.assertEqual(result, [{"stream_id": 1}])
        timeout = _timeout_passed_to(session)
        self.assertIsNotNone(timeout, "get_live_streams called session.get without a timeout kwarg")
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertEqual(timeout.total, LIST_TIMEOUT.total)

    async def test_get_epg_uses_list_timeout(self):
        """get_epg (per-channel backfill) must pass LIST_TIMEOUT."""
        client = _client()
        get_session, session = _mock_session_returning_json({"epg_listings": [{"id": 1}]})
        client._get_session = get_session
        result = await client.get_epg("42")
        self.assertEqual(result, [{"id": 1}])
        timeout = _timeout_passed_to(session)
        self.assertIsNotNone(timeout, "get_epg called session.get without a timeout kwarg")
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertEqual(timeout.total, LIST_TIMEOUT.total)

    async def test_list_timeout_is_longer_than_session_default(self):
        """LIST_TIMEOUT must exceed the 30s session default by a meaningful margin."""
        self.assertGreater(
            LIST_TIMEOUT.total,
            30,
            "LIST_TIMEOUT must be longer than the 30s session default",
        )

    async def test_list_timeout_matches_vod_timeout(self):
        """Heavy list endpoints get at least the same budget VOD lists already had."""
        self.assertGreaterEqual(LIST_TIMEOUT.total, VOD_TIMEOUT.total)


if __name__ == "__main__":
    unittest.main()
