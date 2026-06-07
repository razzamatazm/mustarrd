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


class GetLiveStreamsDictResponseTests(unittest.IsolatedAsyncioTestCase):
    """Providers that return a dict instead of a list must not crash callers."""

    async def test_dict_response_returns_list(self):
        ch1 = {"stream_id": 1, "name": "BBC One", "tv_archive": 1}
        ch2 = {"stream_id": 2, "name": "ITV", "tv_archive": 0}
        client = _client()
        client._get_session = _mock_session({"0": ch1, "1": ch2})
        result = await client.get_live_streams()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIn(ch1, result)
        self.assertIn(ch2, result)

    async def test_list_response_passed_through(self):
        channels = [{"stream_id": 1, "name": "BBC One"}]
        client = _client()
        client._get_session = _mock_session(channels)
        result = await client.get_live_streams()
        self.assertEqual(result, channels)

    async def test_dict_response_is_iterable_with_tv_archive_filter(self):
        ch1 = {"stream_id": 1, "name": "BBC One", "tv_archive": "1"}
        ch2 = {"stream_id": 2, "name": "ITV", "tv_archive": 0}
        client = _client()
        client._get_session = _mock_session({"0": ch1, "1": ch2})
        channels = await client.get_live_streams()
        # This is how epg_ingest_manager filters; must not raise AttributeError
        catchup = [ch for ch in channels if int(ch.get("tv_archive", 0) or 0) == 1]
        self.assertEqual(catchup, [ch1])


class GetLiveCategoriesDictResponseTests(unittest.IsolatedAsyncioTestCase):
    """get_live_categories applies the same dict normalization."""

    async def test_dict_response_returns_list(self):
        cat1 = {"category_id": "1", "category_name": "News"}
        cat2 = {"category_id": "2", "category_name": "Sports"}
        client = _client()
        client._get_session = _mock_session({"0": cat1, "1": cat2})
        result = await client.get_live_categories()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    async def test_list_response_passed_through(self):
        cats = [{"category_id": "1", "category_name": "News"}]
        client = _client()
        client._get_session = _mock_session(cats)
        result = await client.get_live_categories()
        self.assertEqual(result, cats)


if __name__ == "__main__":
    unittest.main()
