import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


def _mock_session(json_data, status=200):
    """Return a patched _get_session that yields a response with the given JSON."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    get_session = AsyncMock(return_value=session)
    return get_session


def _client():
    return XtreamClient("http://provider.test:8080", "user", "pass")


class AuthenticateAuthFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_zero_raises_invalid_credentials(self):
        client = _client()
        client._get_session = _mock_session({"user_info": {"auth": 0, "status": "Disabled"}})
        with self.assertRaises(Exception) as ctx:
            await client.authenticate()
        self.assertIn("Invalid credentials", str(ctx.exception))

    async def test_auth_one_succeeds(self):
        client = _client()
        data = {"user_info": {"auth": 1, "username": "user"}, "server_info": {}}
        client._get_session = _mock_session(data)
        result = await client.authenticate()
        self.assertEqual(result["user_info"]["auth"], 1)

    async def test_auth_key_absent_treated_as_valid(self):
        """Providers that omit the auth field are not rejected."""
        client = _client()
        data = {"user_info": {"username": "user"}, "server_info": {}}
        client._get_session = _mock_session(data)
        result = await client.authenticate()
        self.assertIn("user_info", result)

    async def test_missing_user_info_raises(self):
        client = _client()
        client._get_session = _mock_session({})
        with self.assertRaises(Exception) as ctx:
            await client.authenticate()
        self.assertIn("Invalid response", str(ctx.exception))


class GetEpgNullListingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_epg_null_listings_returns_empty_list(self):
        client = _client()
        client._get_session = _mock_session({"epg_listings": None})
        self.assertEqual(await client.get_epg("1"), [])

    async def test_get_epg_absent_listings_returns_empty_list(self):
        client = _client()
        client._get_session = _mock_session({})
        self.assertEqual(await client.get_epg("1"), [])

    async def test_get_epg_real_listings_passed_through(self):
        client = _client()
        entries = [{"id": "1", "title": "News"}]
        client._get_session = _mock_session({"epg_listings": entries})
        self.assertEqual(await client.get_epg("1"), entries)

    async def test_get_short_epg_null_listings_returns_empty_list(self):
        client = _client()
        client._get_session = _mock_session({"epg_listings": None})
        self.assertEqual(await client.get_short_epg("1"), [])

    async def test_get_short_epg_absent_listings_returns_empty_list(self):
        client = _client()
        client._get_session = _mock_session({})
        self.assertEqual(await client.get_short_epg("1"), [])

    async def test_get_short_epg_real_listings_passed_through(self):
        client = _client()
        entries = [{"id": "2", "title": "Sports"}]
        client._get_session = _mock_session({"epg_listings": entries})
        self.assertEqual(await client.get_short_epg("1"), entries)


if __name__ == "__main__":
    unittest.main()
