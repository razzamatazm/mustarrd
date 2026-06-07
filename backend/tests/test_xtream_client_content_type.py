"""
Regression for: Providers serving JSON as text/plain break all API calls.

aiohttp >= 3.11 enforces content_type='application/json' by default.
PHP-based Xtream panels commonly return text/plain or text/html even for valid JSON.
All response.json() calls must pass content_type=None to accept any content-type.

Each test asserts that response.json was called with content_type=None.
If any call site loses the argument, the test fails.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


def _mock_session_with_spy(json_data, status=200):
    """Return a session mock where response.json is a spy we can inspect."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    get_session = AsyncMock(return_value=session)
    return get_session, response


def _client():
    return XtreamClient("http://provider.test:8080", "user", "pass")


def _assert_content_type_none(response_mock, method_name):
    """Assert response.json was called with content_type=None."""
    response_mock.json.assert_called_once()
    kwargs = response_mock.json.call_args.kwargs
    assert kwargs.get("content_type") is None and "content_type" in kwargs, (
        f"{method_name}: response.json() called without content_type=None. "
        f"Providers returning text/plain will raise ContentTypeError."
    )


class ContentTypeNoneTests(unittest.IsolatedAsyncioTestCase):
    """Each API method must call response.json(content_type=None)."""

    async def test_authenticate_passes_content_type_none(self):
        client = _client()
        data = {"user_info": {"auth": 1}, "server_info": {}}
        get_session, resp = _mock_session_with_spy(data)
        client._get_session = get_session
        await client.authenticate()
        _assert_content_type_none(resp, "authenticate")

    async def test_get_live_categories_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([{"category_id": "1"}])
        client._get_session = get_session
        await client.get_live_categories()
        _assert_content_type_none(resp, "get_live_categories")

    async def test_get_live_streams_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([])
        client._get_session = get_session
        await client.get_live_streams()
        _assert_content_type_none(resp, "get_live_streams")

    async def test_get_epg_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy({"epg_listings": []})
        client._get_session = get_session
        await client.get_epg("1")
        _assert_content_type_none(resp, "get_epg")

    async def test_get_short_epg_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy({"epg_listings": []})
        client._get_session = get_session
        await client.get_short_epg("1")
        _assert_content_type_none(resp, "get_short_epg")

    async def test_get_vod_categories_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([])
        client._get_session = get_session
        await client.get_vod_categories()
        _assert_content_type_none(resp, "get_vod_categories")

    async def test_get_vod_streams_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([])
        client._get_session = get_session
        await client.get_vod_streams()
        _assert_content_type_none(resp, "get_vod_streams")

    async def test_get_vod_info_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy({"info": {}})
        client._get_session = get_session
        await client.get_vod_info("42")
        _assert_content_type_none(resp, "get_vod_info")

    async def test_get_series_categories_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([])
        client._get_session = get_session
        await client.get_series_categories()
        _assert_content_type_none(resp, "get_series_categories")

    async def test_get_series_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy([])
        client._get_session = get_session
        await client.get_series()
        _assert_content_type_none(resp, "get_series")

    async def test_get_series_info_passes_content_type_none(self):
        client = _client()
        get_session, resp = _mock_session_with_spy({"info": {}, "episodes": {}})
        client._get_session = get_session
        await client.get_series_info("7")
        _assert_content_type_none(resp, "get_series_info")


if __name__ == "__main__":
    unittest.main()
