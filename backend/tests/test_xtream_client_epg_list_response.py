import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


def _mock_session(json_data, status=200):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return AsyncMock(return_value=session)


def _client():
    return XtreamClient("http://provider.test:8080", "user", "pass")


class GetEpgListResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_list_response_returns_list(self):
        """Provider returns [] instead of {"epg_listings": []}; must not AttributeError."""
        client = _client()
        client._get_session = _mock_session([])
        result = await client.get_epg("123")
        self.assertEqual(result, [])

    async def test_bare_list_with_entries_returned(self):
        entries = [{"id": "1", "title": "News"}, {"id": "2", "title": "Sport"}]
        client = _client()
        client._get_session = _mock_session(entries)
        result = await client.get_epg("123")
        self.assertEqual(result, entries)

    async def test_dict_response_extracts_epg_listings(self):
        entries = [{"id": "1", "title": "News"}]
        client = _client()
        client._get_session = _mock_session({"epg_listings": entries})
        result = await client.get_epg("123")
        self.assertEqual(result, entries)

    async def test_dict_response_null_epg_listings_returns_empty(self):
        client = _client()
        client._get_session = _mock_session({"epg_listings": None})
        result = await client.get_epg("123")
        self.assertEqual(result, [])

    async def test_dict_response_dict_epg_listings_returns_list(self):
        # Some providers return epg_listings as {"0": {...}, "1": {...}} instead
        # of a list.  get_epg must coerce to list; returning a dict causes
        # _backfill_from_api to silently skip all entries (dict keys are strings,
        # not dicts) and fetch_live_epg to raise AttributeError on .get() calls.
        entries = {"0": {"id": "1", "title": "News"}, "1": {"id": "2", "title": "Sport"}}
        client = _client()
        client._get_session = _mock_session({"epg_listings": entries})
        result = await client.get_epg("123")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertEqual({e["title"] for e in result}, {"News", "Sport"})


class GetShortEpgListResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_list_response_returns_list(self):
        """Provider returns [] instead of {"epg_listings": []}; must not AttributeError."""
        client = _client()
        client._get_session = _mock_session([])
        result = await client.get_short_epg("123")
        self.assertEqual(result, [])

    async def test_bare_list_with_entries_returned(self):
        entries = [{"id": "1", "title": "Movie"}]
        client = _client()
        client._get_session = _mock_session(entries)
        result = await client.get_short_epg("123")
        self.assertEqual(result, entries)

    async def test_dict_response_extracts_epg_listings(self):
        entries = [{"id": "1", "title": "Movie"}]
        client = _client()
        client._get_session = _mock_session({"epg_listings": entries})
        result = await client.get_short_epg("123")
        self.assertEqual(result, entries)

    async def test_dict_response_null_epg_listings_returns_empty(self):
        client = _client()
        client._get_session = _mock_session({"epg_listings": None})
        result = await client.get_short_epg("123")
        self.assertEqual(result, [])

    async def test_dict_response_dict_epg_listings_returns_list(self):
        # Same coercion bug as get_epg: dict epg_listings must be converted to list.
        entries = {"0": {"id": "1", "title": "Movie"}, "1": {"id": "2", "title": "Sport"}}
        client = _client()
        client._get_session = _mock_session({"epg_listings": entries})
        result = await client.get_short_epg("123")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertEqual({e["title"] for e in result}, {"Movie", "Sport"})


if __name__ == "__main__":
    unittest.main()
