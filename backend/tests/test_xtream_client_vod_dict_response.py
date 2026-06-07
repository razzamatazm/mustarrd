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


class GetVodCategoriesDictResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dict_response_returns_list(self):
        cat1 = {"category_id": "1", "category_name": "Action"}
        cat2 = {"category_id": "2", "category_name": "Comedy"}
        client = _client()
        client._get_session = _mock_session({"0": cat1, "1": cat2})
        result = await client.get_vod_categories()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIn(cat1, result)
        self.assertIn(cat2, result)

    async def test_list_response_passed_through(self):
        cats = [{"category_id": "1", "category_name": "Action"}]
        client = _client()
        client._get_session = _mock_session(cats)
        result = await client.get_vod_categories()
        self.assertEqual(result, cats)


class GetVodStreamsDictResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dict_response_returns_list(self):
        m1 = {"stream_id": 10, "name": "The Matrix"}
        m2 = {"stream_id": 11, "name": "Inception"}
        client = _client()
        client._get_session = _mock_session({"0": m1, "1": m2})
        result = await client.get_vod_streams()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIn(m1, result)
        self.assertIn(m2, result)

    async def test_list_response_passed_through(self):
        movies = [{"stream_id": 10, "name": "The Matrix"}]
        client = _client()
        client._get_session = _mock_session(movies)
        result = await client.get_vod_streams()
        self.assertEqual(result, movies)


class GetSeriesCategoriesDictResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dict_response_returns_list(self):
        cat1 = {"category_id": "5", "category_name": "Drama"}
        cat2 = {"category_id": "6", "category_name": "Sci-Fi"}
        client = _client()
        client._get_session = _mock_session({"0": cat1, "1": cat2})
        result = await client.get_series_categories()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIn(cat1, result)
        self.assertIn(cat2, result)

    async def test_list_response_passed_through(self):
        cats = [{"category_id": "5", "category_name": "Drama"}]
        client = _client()
        client._get_session = _mock_session(cats)
        result = await client.get_series_categories()
        self.assertEqual(result, cats)


class GetSeriesDictResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_dict_response_returns_list(self):
        s1 = {"series_id": 20, "name": "Breaking Bad"}
        s2 = {"series_id": 21, "name": "The Wire"}
        client = _client()
        client._get_session = _mock_session({"0": s1, "1": s2})
        result = await client.get_series()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        self.assertIn(s1, result)
        self.assertIn(s2, result)

    async def test_list_response_passed_through(self):
        series = [{"series_id": 20, "name": "Breaking Bad"}]
        client = _client()
        client._get_session = _mock_session(series)
        result = await client.get_series()
        self.assertEqual(result, series)


if __name__ == "__main__":
    unittest.main()
