"""
Regression for #453: adding an account failed with "Provider connection failed"
on a provider whose credentials worked in every other app.

Root cause: nothing Mustarrd sent to a provider carried a User-Agent of its
own, so every request went out as aiohttp's default ("Python/3.x aiohttp/3.x").
Some providers drop the connection for anything that does not look like a
media player, which surfaced as ``ConnectionResetError`` inside
``XtreamClient.authenticate``.

Fix: every request to a provider identifies itself as a media player, with
``CATCHUP_PROVIDER_USER_AGENT`` as the override for providers that want a
specific one.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from aiohttp import web

from api.channels import _ProviderStream
from config import settings as app_settings
from services.download_manager import DownloadManager
from services.provider_http import (
    DEFAULT_PROVIDER_USER_AGENT,
    provider_request_headers,
    provider_user_agent,
)
from services.xtream_client import XtreamClient


class _RecordingProvider:
    """A real HTTP server standing in for a provider; records what it was sent."""

    def __init__(self):
        self.user_agents = []
        self._runner = None
        self.base_url = ""

    async def _player_api(self, request):
        self.user_agents.append(request.headers.get("User-Agent", ""))
        return web.json_response({"user_info": {"auth": 1}, "server_info": {}})

    async def start(self):
        app = web.Application()
        app.router.add_get("/player_api.php", self._player_api)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()


class ProviderUserAgentSettingTests(unittest.TestCase):
    def test_default_looks_like_a_media_player_not_a_script(self):
        agent = provider_user_agent()

        self.assertEqual(agent, DEFAULT_PROVIDER_USER_AGENT)
        self.assertNotIn("aiohttp", agent.lower())
        self.assertNotIn("python", agent.lower())

    def test_configured_value_replaces_the_default(self):
        with patch.object(app_settings, "provider_user_agent", "TiviMate/5.1.6 (Android 12)"):
            self.assertEqual(provider_user_agent(), "TiviMate/5.1.6 (Android 12)")
            self.assertEqual(
                provider_request_headers(), {"User-Agent": "TiviMate/5.1.6 (Android 12)"}
            )

    def test_blank_configured_value_falls_back_to_the_default(self):
        """An empty env var must not send an empty User-Agent header."""
        with patch.object(app_settings, "provider_user_agent", "   "):
            self.assertEqual(provider_user_agent(), DEFAULT_PROVIDER_USER_AGENT)

    def test_a_line_break_cannot_smuggle_in_a_second_header(self):
        with patch.object(app_settings, "provider_user_agent", "VLC/3.0\r\nX-Evil: 1"):
            self.assertEqual(provider_user_agent(), DEFAULT_PROVIDER_USER_AGENT)


class XtreamClientUserAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = _RecordingProvider()
        await self.provider.start()
        self.addAsyncCleanup(self.provider.stop)

    async def _authenticate(self):
        client = XtreamClient(self.provider.base_url, "user", "pass")
        try:
            await client.authenticate()
        finally:
            await client.close()

    async def test_account_login_identifies_as_a_media_player_on_the_wire(self):
        await self._authenticate()

        self.assertEqual(self.provider.user_agents, [DEFAULT_PROVIDER_USER_AGENT])

    async def test_configured_user_agent_is_what_the_provider_receives(self):
        with patch.object(app_settings, "provider_user_agent", "TiviMate/5.1.6 (Android 12)"):
            await self._authenticate()

        self.assertEqual(self.provider.user_agents, ["TiviMate/5.1.6 (Android 12)"])


def _session_factory_for(response):
    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=response)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    http_session = MagicMock()
    http_session.get.return_value = get_cm

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=http_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=client_cm)


class DownloadUserAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_download_identifies_as_a_media_player(self):
        body = b"\x47" * 256
        response = MagicMock()
        response.status = 200
        response.content_length = len(body)
        response.reason = "OK"
        response.headers = {}

        async def iter_chunked(_size):
            yield body

        response.content.iter_chunked = iter_chunked
        session_factory = _session_factory_for(response)

        manager = DownloadManager()
        db_session = AsyncMock()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("services.download_manager.aiohttp.ClientSession", session_factory),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                await manager._download_file_once(
                    "http://provider/stream", str(Path(tmp) / "out.ts"), 1, db_session, offset=0
                )

        _, kwargs = session_factory.call_args
        self.assertEqual(kwargs["headers"], {"User-Agent": DEFAULT_PROVIDER_USER_AGENT})


class LivePreviewUserAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_stream_identifies_as_a_media_player(self):
        response = MagicMock()
        response.status = 200
        http_session = MagicMock()
        http_session.get = AsyncMock(return_value=response)
        http_session.closed = False
        http_session.close = AsyncMock()
        session_factory = MagicMock(return_value=http_session)

        with patch("api.channels.aiohttp.ClientSession", session_factory):
            await _ProviderStream("http://provider/live/u/p/1.ts").open()

        _, kwargs = session_factory.call_args
        self.assertEqual(kwargs["headers"], {"User-Agent": DEFAULT_PROVIDER_USER_AGENT})


class EveryProviderSessionSendsTheUserAgentTests(unittest.TestCase):
    """A new request site that forgets the header would bring #453 back quietly."""

    # Sessions that talk to something other than an IPTV provider.
    # update_check.py talks to api.github.com and names its own User-Agent.
    NOT_PROVIDER_FACING = {"plex_service.py", "webhook_dispatcher.py", "update_check.py"}

    def test_no_provider_session_is_opened_without_the_headers(self):
        import ast

        missing = []
        for folder in ("services", "api"):
            for path in sorted((BACKEND_ROOT / folder).glob("*.py")):
                if path.name in self.NOT_PROVIDER_FACING:
                    continue
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if not (isinstance(func, ast.Attribute) and func.attr == "ClientSession"):
                        continue
                    sends_headers = any(
                        keyword.arg == "headers"
                        and isinstance(keyword.value, ast.Call)
                        and getattr(keyword.value.func, "id", "") == "provider_request_headers"
                        for keyword in node.keywords
                    )
                    if not sends_headers:
                        missing.append(f"{folder}/{path.name}:{node.lineno}")

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
