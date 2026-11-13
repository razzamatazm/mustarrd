"""
Tests for the channel logo cache (services/logo_cache.py) and its endpoint.

Provider logo hosts rarely send cache headers, so GET /api/logos proxies
each logo through a disk cache. The service must:

- fetch a logo once and serve repeats from disk,
- negative-cache failures so dead URLs don't hammer the provider,
- reject non-http(s) and private/loopback URLs (SSRF guard),
- cap the body size,
- fall back to a safe content type when the host lies about it.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException

import services.logo_cache as logo_cache_module
from services.logo_cache import LogoCache, LOGO_MAX_BYTES, _validate_logo_url


LOGO_URL = "http://logos.example.com/channel.png"


def _make_response(status=200, body=b"\x89PNG-bytes", content_type="image/png", headers=None):
    response = MagicMock()
    response.status = status
    if headers is None:
        headers = {"Content-Type": content_type}
    response.headers = headers
    response.content_length = len(body)

    async def iter_chunked(_size):
        yield body

    response.content.iter_chunked = iter_chunked
    return response


class _ResponseContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class _FakeHttpSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested_urls = []

    def get(self, url, allow_redirects=False):
        self.requested_urls.append(url)
        return _ResponseContext(self._responses.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class LogoCacheTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(
            logo_cache_module, "ensure_config_files", return_value=Path(self._tmp.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.session_factory_calls = 0

    def _patch_http(self, responses):
        def factory(*args, **kwargs):
            self.session_factory_calls += 1
            self.session_factory_kwargs = kwargs
            return _FakeHttpSession(responses)

        patcher = patch.object(logo_cache_module.aiohttp, "ClientSession", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_url_validation(self, allowed=True):
        async def validate(_url):
            return allowed

        patcher = patch.object(logo_cache_module, "_validate_logo_url", validate)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestLogoFetchAndCache(LogoCacheTestBase):
    def test_second_request_served_from_disk_without_refetch(self):
        self._patch_url_validation()
        self._patch_http([_make_response(body=b"logo-bytes")])
        cache = LogoCache()

        first = asyncio.run(cache.get_logo(LOGO_URL))
        second = asyncio.run(cache.get_logo(LOGO_URL))

        self.assertEqual(first, (b"logo-bytes", "image/png"))
        self.assertEqual(second, (b"logo-bytes", "image/png"))
        self.assertEqual(self.session_factory_calls, 1)

    def test_cache_survives_new_cache_instance(self):
        self._patch_url_validation()
        self._patch_http([_make_response(body=b"logo-bytes")])

        asyncio.run(LogoCache().get_logo(LOGO_URL))
        result = asyncio.run(LogoCache().get_logo(LOGO_URL))

        self.assertEqual(result, (b"logo-bytes", "image/png"))
        self.assertEqual(self.session_factory_calls, 1)

    def test_logo_fetch_identifies_as_a_media_player(self):
        """#453: logos are often served by the provider, behind the same filter."""
        from services.provider_http import DEFAULT_PROVIDER_USER_AGENT

        self._patch_url_validation()
        self._patch_http([_make_response(body=b"logo-bytes")])

        asyncio.run(LogoCache().get_logo(LOGO_URL))

        self.assertEqual(
            self.session_factory_kwargs["headers"],
            {"User-Agent": DEFAULT_PROVIDER_USER_AGENT},
        )

    def test_failed_fetch_is_negative_cached(self):
        self._patch_url_validation()
        self._patch_http([_make_response(status=404)])
        cache = LogoCache()

        self.assertIsNone(asyncio.run(cache.get_logo(LOGO_URL)))
        # Second call within the negative-cache window must not refetch
        self.assertIsNone(asyncio.run(cache.get_logo(LOGO_URL)))
        self.assertEqual(self.session_factory_calls, 1)

    def test_non_image_content_type_falls_back_to_octet_stream(self):
        self._patch_url_validation()
        self._patch_http([_make_response(content_type="text/html")])
        cache = LogoCache()

        result = asyncio.run(cache.get_logo(LOGO_URL))

        self.assertIsNotNone(result)
        self.assertEqual(result[1], "application/octet-stream")

    def test_oversized_body_is_rejected(self):
        self._patch_url_validation()
        big = b"x" * (LOGO_MAX_BYTES + 1)
        response = _make_response(body=big)
        response.content_length = None  # no declared length; stream cap must catch it
        self._patch_http([response])
        cache = LogoCache()

        self.assertIsNone(asyncio.run(cache.get_logo(LOGO_URL)))

    def test_redirect_is_followed_and_revalidated(self):
        self._patch_url_validation()
        redirect = _make_response(status=302, headers={"Location": "http://cdn.example.com/c.png"})
        self._patch_http([redirect, _make_response(body=b"after-redirect")])
        cache = LogoCache()

        result = asyncio.run(cache.get_logo(LOGO_URL))

        self.assertEqual(result[0], b"after-redirect")


class TestLogoUrlValidation(LogoCacheTestBase):
    def test_rejects_non_http_schemes(self):
        self.assertFalse(asyncio.run(_validate_logo_url("ftp://logos.example.com/a.png")))
        self.assertFalse(asyncio.run(_validate_logo_url("file:///etc/passwd")))

    def test_rejects_loopback_and_private_hosts(self):
        self.assertFalse(asyncio.run(_validate_logo_url("http://127.0.0.1/logo.png")))
        self.assertFalse(asyncio.run(_validate_logo_url("http://localhost/logo.png")))
        self.assertFalse(asyncio.run(_validate_logo_url("http://192.168.1.10/logo.png")))

    def test_private_url_never_reaches_http(self):
        self._patch_http([_make_response()])
        cache = LogoCache()

        result = asyncio.run(cache.get_logo("http://127.0.0.1/logo.png"))

        self.assertIsNone(result)
        self.assertEqual(self.session_factory_calls, 0)


class TestLogoEndpoint(unittest.TestCase):
    def test_serves_cached_logo_with_long_lived_cache_headers(self):
        from api.channels import get_channel_logo

        async def fake_get_logo(_url):
            return b"logo-bytes", "image/png"

        with patch.object(logo_cache_module.logo_cache, "get_logo", fake_get_logo):
            response = asyncio.run(get_channel_logo(url=LOGO_URL))

        self.assertEqual(response.body, b"logo-bytes")
        self.assertEqual(response.media_type, "image/png")
        self.assertIn("max-age=604800", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])

    def test_unavailable_logo_returns_404(self):
        from api.channels import get_channel_logo

        async def fake_get_logo(_url):
            return None

        with patch.object(logo_cache_module.logo_cache, "get_logo", fake_get_logo):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(get_channel_logo(url=LOGO_URL))

        self.assertEqual(ctx.exception.status_code, 404)

    def test_logo_route_requires_auth_dependency(self):
        import api.channels as channels_api
        from auth import require_admin_or_download_user

        route = next(
            r for r in channels_api.router.routes if getattr(r, "path", None) == "/logos"
        )
        dependencies = [d.call for d in route.dependant.dependencies]
        self.assertIn(require_admin_or_download_user, dependencies)


if __name__ == "__main__":
    unittest.main()
