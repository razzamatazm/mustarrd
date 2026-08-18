"""
Tests for VOD preview: checking a movie or an episode before downloading it.

Unlike a live preview, a VOD preview must seek, which means FFmpeg needs a
seekable source. It gets one from a loopback relay this process serves, guarded
by a short-lived token — because the provider URL it stands in for embeds the
account username and password and must never reach the browser, FFmpeg's argv,
or an error message.

What these cover:

- movie and episode URLs are built with the right container extension,
- the relay passes ranges through, refuses non-loopback callers, and refuses
  unknown or expired tokens,
- credentials appear in no response, header or FFmpeg argument,
- `?start=` restarts the session at the offset rather than reusing offset 0,
- VOD stops at a 15-minute wall-clock ceiling while live/catchup keep 300s,
- VOD previews share the live preview's concurrency budget.
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import api.channels as channels_api
import api.vod as vod_api
from api.vod import (
    VOD_PREVIEW_MAX_SECONDS,
    vod_preview_duration,
    vod_preview_hls_asset,
    vod_preview_source,
)
from auth import AuthContext, require_admin_or_download_user
from database import Base
from models import XtreamAccount
from services.hls_streamer import HLSSession, HLSStartError, HLSUnavailableError
from services.preview_budget import preview_budget
from services.vod_preview_source import VodPreviewSourceRelay, loopback_source_url

# Markers, not credentials: the tests assert these strings appear in no
# response, header or FFmpeg argument. Assembled rather than written out as
# literals so secret scanners have no password-shaped assignment to flag —
# a new file full of leak assertions is exactly what trips them.
SECRET_PASSWORD = "-".join(["canary", "provider", "pw"])
SECRET_USERNAME = "-".join(["canary", "provider", "user"])
SERVER_URL = "http://provider.example:8080"
LOOPBACK_PORT = 4177


def _make_auth():
    return AuthContext(authenticated=True, role="download_only", user_id=1, is_admin=False)


def _make_request(client_host="127.0.0.1", headers=None, port=LOOPBACK_PORT):
    request = MagicMock()
    request.client = MagicMock(host=client_host) if client_host is not None else None
    request.headers = headers or {}
    request.scope = {"server": ("0.0.0.0", port)}
    return request


class _AccountDbTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        preview_budget.active = 0
        vod_api._preview_ceilings.clear()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_factory() as session:
            account = XtreamAccount(
                name="Provider",
                server_url=SERVER_URL,
                username=SECRET_USERNAME,
                password="",
                password_encrypted="enc",
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            self.account_id = account.id

    async def asyncTearDown(self):
        preview_budget.active = 0
        vod_api._preview_ceilings.clear()
        await self.engine.dispose()


class SourceTokenTests(unittest.TestCase):
    """The token stands in for a credentialed URL, so it must be unguessable,
    expire, and die when its session does."""

    def test_token_resolves_to_the_url_it_was_minted_for(self):
        relay = VodPreviewSourceRelay()
        token = relay.mint("http://provider.example/movie/u/p/1.mkv")
        self.assertEqual(relay.resolve(token), "http://provider.example/movie/u/p/1.mkv")

    def test_tokens_are_long_and_distinct(self):
        relay = VodPreviewSourceRelay()
        tokens = {relay.mint("http://x/1") for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_unknown_token_resolves_to_nothing(self):
        self.assertIsNone(VodPreviewSourceRelay().resolve("not-a-token"))

    def test_expired_token_resolves_to_nothing(self):
        relay = VodPreviewSourceRelay(ttl_seconds=-1)
        token = relay.mint("http://provider.example/movie/u/p/1.mkv")
        self.assertIsNone(relay.resolve(token))

    def test_revoked_token_resolves_to_nothing(self):
        relay = VodPreviewSourceRelay()
        token = relay.mint("http://provider.example/movie/u/p/1.mkv")
        relay.revoke(token)
        self.assertIsNone(relay.resolve(token))
        relay.revoke(token)  # teardown paths are idempotent

    def test_expired_grants_do_not_accumulate(self):
        relay = VodPreviewSourceRelay(ttl_seconds=-1)
        for _ in range(5):
            relay.mint("http://provider.example/movie/u/p/1.mkv")
        relay.mint("http://provider.example/movie/u/p/2.mkv")
        self.assertEqual(relay.active_count, 1)

    def test_loopback_url_carries_the_token_and_nothing_else(self):
        url = loopback_source_url(4177, "tok3n")
        self.assertEqual(url, "http://127.0.0.1:4177/api/vod/preview/source/tok3n")


class SourceRelayTests(unittest.IsolatedAsyncioTestCase):
    """The relay is FFmpeg-facing, not browser-facing."""

    def setUp(self):
        self.relay = VodPreviewSourceRelay()
        self.token = self.relay.mint(f"{SERVER_URL}/movie/{SECRET_USERNAME}/{SECRET_PASSWORD}/9.mkv")

    async def _call(self, request, provider_status=200, provider_headers=None, chunks=(b"data",)):
        provider = MagicMock()
        provider.status = provider_status
        provider.headers = provider_headers or {}
        provider.close = MagicMock()

        async def iter_chunked(_size):
            for chunk in chunks:
                yield chunk

        provider.content.iter_chunked = iter_chunked

        http_session = MagicMock()
        http_session.get = AsyncMock(return_value=provider)
        http_session.closed = False
        http_session.close = AsyncMock()
        self.provider = provider
        self.http_session = http_session

        with (
            patch("api.vod.vod_preview_source_relay", self.relay),
            patch("api.vod.aiohttp.ClientSession", return_value=http_session),
        ):
            return await vod_preview_source(token=self.token, request=request)

    def test_relay_needs_no_session_auth(self):
        """FFmpeg has no cookie; loopback plus the token is the guard."""
        sig = inspect.signature(vod_preview_source)
        self.assertNotIn("_auth", sig.parameters)

    async def test_lan_caller_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(_make_request(client_host="192.168.1.50"))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_unknown_caller_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(_make_request(client_host=None))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_forwarded_request_is_refused(self):
        """A reverse proxy sharing this process's network namespace makes every
        request look local, so the peer address alone stops distinguishing
        anything. FFmpeg sends no proxy headers; a proxy adds them."""
        for header in ["x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded"]:
            with self.assertRaises(HTTPException) as ctx:
                await self._call(_make_request(headers={header: "203.0.113.7"}))
            self.assertEqual(ctx.exception.status_code, 403, header)

    async def test_a_range_header_is_not_mistaken_for_a_proxy_header(self):
        response = await self._call(
            _make_request(headers={"range": "bytes=0-99"}),
            provider_status=206,
            provider_headers={"Content-Range": "bytes 0-99/500"},
        )
        self.assertEqual(response.status_code, 206)

    async def test_unknown_token_is_refused(self):
        self.token = "not-a-token"
        with self.assertRaises(HTTPException) as ctx:
            await self._call(_make_request())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_expired_token_is_refused(self):
        self.relay = VodPreviewSourceRelay(ttl_seconds=-1)
        self.token = self.relay.mint(f"{SERVER_URL}/movie/u/p/9.mkv")
        with self.assertRaises(HTTPException) as ctx:
            await self._call(_make_request())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_range_is_passed_through_in_both_directions(self):
        """This is what makes the source seekable, and the preview scrubbable."""
        response = await self._call(
            _make_request(headers={"range": "bytes=1000-1999"}),
            provider_status=206,
            provider_headers={
                "Content-Range": "bytes 1000-1999/500000",
                "Content-Length": "1000",
                "Content-Type": "video/x-matroska",
            },
        )
        self.assertEqual(
            self.http_session.get.await_args.kwargs["headers"]["Range"], "bytes=1000-1999"
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 1000-1999/500000")
        self.assertEqual(response.headers["accept-ranges"], "bytes")

    async def test_range_beyond_eof_is_passed_through_as_416(self):
        response = await self._call(
            _make_request(headers={"range": "bytes=999999999-"}),
            provider_status=416,
            provider_headers={"Content-Range": "bytes */500000"},
        )
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */500000")
        await asyncio.sleep(0)  # the connection close runs as a detached task
        self.provider.close.assert_called_once()

    async def test_whole_file_request_relays_the_bytes(self):
        response = await self._call(
            _make_request(),
            provider_headers={"Content-Length": "4", "Content-Type": "video/mp4"},
            chunks=(b"ab", b"cd"),
        )
        body = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual(body, b"abcd")
        self.assertEqual(response.headers["content-length"], "4")
        await asyncio.sleep(0)  # the connection close runs as a detached task
        self.provider.close.assert_called_once()
        self.http_session.close.assert_awaited()

    async def test_provider_refusal_never_echoes_the_url(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(_make_request(), provider_status=403)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertNotIn(SECRET_PASSWORD, ctx.exception.detail)
        self.assertNotIn(SECRET_USERNAME, ctx.exception.detail)
        self.assertNotIn("provider.example", ctx.exception.detail)

    async def test_relay_response_carries_no_credentials(self):
        response = await self._call(_make_request())
        for name, value in response.headers.items():
            self.assertNotIn(SECRET_PASSWORD, value, f"Credential leaked in header {name}")
            self.assertNotIn(SECRET_USERNAME, value, f"Credential leaked in header {name}")


class VodPreviewHlsTests(_AccountDbTest):
    """GET .../preview/{account}/{kind}/{item}/hls/{asset}"""

    def _make_hls_session(self, on_close=None, fingerprint=""):
        directory = Path(tempfile.mkdtemp(prefix="vod-preview-test-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        session = HLSSession(
            key="vodpreview:1:movie:42",
            directory=directory,
            live=True,
            fingerprint=fingerprint,
        )
        session.on_close = on_close
        session.playlist_path.write_text("#EXTM3U\nseg00000.m4s\n")
        return session

    def _streamer(self, hls_session, active=None):
        streamer = MagicMock()
        streamer.get_active.return_value = active

        async def start(_key, _url, _fingerprint, _max_seconds, start_offset=0.0, on_close=None):
            hls_session.on_close = on_close
            return hls_session

        streamer.get_or_create_url = AsyncMock(side_effect=start)
        streamer.wait_for_playlist = AsyncMock()
        streamer.close_session = AsyncMock()
        return streamer

    async def _call(self, asset="playlist.m3u8", kind="movie", item_id="42",
                    streamer=None, relay=None, **kwargs):
        with (
            patch("api.vod.hls_streamer", streamer or MagicMock()),
            patch("api.vod.vod_preview_source_relay", relay or VodPreviewSourceRelay()),
            patch(
                "api.vod.resolve_account_password_with_migration",
                AsyncMock(return_value=SECRET_PASSWORD),
            ),
        ):
            async with self.session_factory() as session:
                return await vod_preview_hls_asset(
                    account_id=kwargs.pop("account_id", self.account_id),
                    request=kwargs.pop("request", _make_request()),
                    kind=kind,
                    item_id=item_id,
                    asset=asset,
                    container_extension=kwargs.pop("container_extension", "mkv"),
                    start=kwargs.pop("start", 0.0),
                    _auth=_make_auth(),
                    session=session,
                )

    def test_requires_authentication(self):
        auth_param = inspect.signature(vod_preview_hls_asset).parameters.get("_auth")
        self.assertIsNotNone(auth_param, "_auth parameter missing from handler")
        self.assertEqual(auth_param.default.dependency, require_admin_or_download_user)

    async def test_unknown_asset_name_404(self):
        for asset in ["../../etc/passwd", "ffmpeg.log", "probe.ts"]:
            with self.assertRaises(HTTPException) as ctx:
                await self._call(asset=asset)
            self.assertEqual(ctx.exception.status_code, 404, asset)

    async def test_segment_without_session_409(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(asset="seg00000.m4s", streamer=self._streamer(None))
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_movie_url_uses_the_movie_path_and_container(self):
        relay = VodPreviewSourceRelay()
        await self._call(streamer=self._streamer(self._make_hls_session()), relay=relay)
        (grant,) = relay._grants.values()
        self.assertEqual(
            grant.url,
            f"{SERVER_URL}/movie/{SECRET_USERNAME}/{SECRET_PASSWORD}/42.mkv",
        )

    async def test_episode_url_uses_the_series_path_and_container(self):
        relay = VodPreviewSourceRelay()
        await self._call(
            kind="episode", item_id="907", container_extension="mp4",
            streamer=self._streamer(self._make_hls_session()), relay=relay,
        )
        (grant,) = relay._grants.values()
        self.assertEqual(
            grant.url,
            f"{SERVER_URL}/series/{SECRET_USERNAME}/{SECRET_PASSWORD}/907.mp4",
        )

    async def test_ffmpeg_is_given_the_loopback_relay_not_the_provider_url(self):
        """argv is readable by every user on the host via `ps`."""
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer)

        url = streamer.get_or_create_url.await_args.args[1]
        self.assertTrue(url.startswith(f"http://127.0.0.1:{LOOPBACK_PORT}/"), url)
        self.assertNotIn(SECRET_PASSWORD, url)
        self.assertNotIn(SECRET_USERNAME, url)
        self.assertNotIn("provider.example", url)

        fingerprint = streamer.get_or_create_url.await_args.args[2]
        self.assertNotIn(SECRET_PASSWORD, fingerprint)
        self.assertNotIn(SECRET_USERNAME, fingerprint)

    async def test_works_behind_a_reverse_proxy(self):
        """The common deployment. The relay URL must name the port this process
        is actually bound to — read off the socket the request arrived on —
        rather than anything the proxy put in the Host header, and the playlist
        request itself must not be refused for carrying proxy headers (only the
        FFmpeg-facing relay checks for those)."""
        streamer = self._streamer(self._make_hls_session())
        proxied = _make_request(
            headers={
                "host": "mustarrd.example.com",
                "x-forwarded-for": "203.0.113.9",
                "x-forwarded-proto": "https",
            },
            port=4177,
        )

        await self._call(streamer=streamer, request=proxied)

        url = streamer.get_or_create_url.await_args.args[1]
        self.assertEqual(url.split("/api/")[0], "http://127.0.0.1:4177")
        self.assertNotIn("mustarrd.example.com", url)

    async def test_playlist_response_has_no_credentials(self):
        response = await self._call(streamer=self._streamer(self._make_hls_session()))
        for name, value in response.headers.items():
            self.assertNotIn(SECRET_PASSWORD, value, f"Credential leaked in header {name}")
            self.assertNotIn(SECRET_USERNAME, value, f"Credential leaked in header {name}")
            self.assertNotIn("provider.example", value, f"Provider URL leaked in header {name}")

    async def test_vod_gets_a_wall_clock_ceiling_not_the_live_cap(self):
        """Once a preview can seek, a 300s media cap would mean never seeing
        past the first five minutes of a film."""
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer)
        self.assertAlmostEqual(
            streamer.get_or_create_url.await_args.args[3], VOD_PREVIEW_MAX_SECONDS, delta=5
        )
        self.assertEqual(VOD_PREVIEW_MAX_SECONDS, 900)
        self.assertEqual(channels_api.PREVIEW_MAX_SECONDS, 300, "Live/catchup cap changed")

    async def test_scrubbing_does_not_restart_the_ceiling(self):
        """The ceiling belongs to the preview, not to the FFmpeg process behind
        it. Otherwise nudging the scrub bar every fourteen minutes would let
        you watch a whole film through something that is not a player."""
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer)

        key = "vodpreview:%s:movie:42" % self.account_id
        # Pretend ten of the fifteen minutes have already gone.
        vod_api._preview_ceilings[key] -= 600
        await self._call(streamer=streamer, start=3600.0)

        self.assertAlmostEqual(
            streamer.get_or_create_url.await_args.args[3], 300, delta=5,
            msg="Scrubbing handed the viewer a fresh ceiling.",
        )

    async def test_preview_is_refused_once_its_ceiling_has_passed(self):
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer)

        key = "vodpreview:%s:movie:42" % self.account_id
        vod_api._preview_ceilings[key] -= VOD_PREVIEW_MAX_SECONDS
        streamer.get_or_create_url.reset_mock()

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer, start=60.0)

        self.assertEqual(ctx.exception.status_code, 429)
        streamer.get_or_create_url.assert_not_awaited()
        # Refused before the budget was touched: the first preview's slot is
        # the only one outstanding.
        self.assertEqual(preview_budget.active, 1)

    async def test_a_stale_ceiling_eventually_lets_a_new_preview_start(self):
        """The lockout is a stop, not a ban."""
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer)

        key = "vodpreview:%s:movie:42" % self.account_id
        vod_api._preview_ceilings[key] -= (
            VOD_PREVIEW_MAX_SECONDS + vod_api.PREVIEW_CEILING_LOCKOUT_SECONDS + 1
        )
        await self._call(streamer=streamer, start=60.0)

        self.assertAlmostEqual(
            streamer.get_or_create_url.await_args.args[3], VOD_PREVIEW_MAX_SECONDS, delta=5
        )

    async def test_start_offset_reaches_the_streamer(self):
        streamer = self._streamer(self._make_hls_session())
        await self._call(streamer=streamer, start=3600.0)
        self.assertEqual(streamer.get_or_create_url.await_args.kwargs["start_offset"], 3600.0)

    async def test_start_is_part_of_the_fingerprint_so_scrubbing_restarts(self):
        """A session sitting at zero must not be served to someone who asked
        for the one-hour mark."""
        at_zero = self._make_hls_session(fingerprint="movie:42:mkv:0.000")
        streamer = self._streamer(self._make_hls_session(), active=at_zero)

        await self._call(streamer=streamer, start=3600.0)

        streamer.get_or_create_url.assert_awaited_once()
        self.assertEqual(
            streamer.get_or_create_url.await_args.args[2], "movie:42:mkv:3600.000"
        )

    async def test_matching_session_is_reused_without_a_second_slot(self):
        existing = self._make_hls_session(fingerprint="movie:42:mkv:0.000")
        streamer = self._streamer(existing, active=existing)

        await self._call(streamer=streamer)

        streamer.get_or_create_url.assert_not_awaited()
        self.assertEqual(preview_budget.active, 0)
        streamer.touch.assert_called_once_with(existing)

    async def test_shares_the_live_preview_budget(self):
        preview_budget.active = preview_budget.limit
        streamer = self._streamer(self._make_hls_session())

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)

        self.assertEqual(ctx.exception.status_code, 429)
        streamer.get_or_create_url.assert_not_awaited()
        self.assertEqual(preview_budget.active, preview_budget.limit)

    async def test_losing_the_start_race_gives_the_slot_back(self):
        hls_session = self._make_hls_session()
        streamer = self._streamer(hls_session)

        async def reuse(_key, _url, _fingerprint, _max, start_offset=0.0, on_close=None):
            on_close()  # not adopted: an equivalent session already existed
            return hls_session

        streamer.get_or_create_url = AsyncMock(side_effect=reuse)

        await self._call(streamer=streamer)

        self.assertEqual(preview_budget.active, 0)

    async def test_failed_start_releases_the_slot_and_revokes_the_token(self):
        relay = VodPreviewSourceRelay()
        streamer = self._streamer(None)
        streamer.get_or_create_url = AsyncMock(side_effect=HLSStartError("no video track"))

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer, relay=relay)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(preview_budget.active, 0, "A failed VOD preview leaked its slot.")
        self.assertEqual(relay.active_count, 0, "A failed VOD preview left a live token.")

    async def test_ffmpeg_missing_returns_503(self):
        streamer = self._streamer(None)
        streamer.get_or_create_url = AsyncMock(side_effect=HLSUnavailableError("no ffmpeg"))
        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(preview_budget.active, 0)

    async def test_playlist_timeout_tears_down_its_own_session(self):
        mine = self._make_hls_session()
        streamer = self._streamer(mine)
        streamer.wait_for_playlist = AsyncMock(side_effect=HLSStartError("timed out"))

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(preview_budget.active, 0)
        streamer.close_session.assert_awaited_once_with(mine)

    async def test_session_close_revokes_the_token(self):
        """The grant must not outlive the session it was minted for."""
        relay = VodPreviewSourceRelay()
        hls_session = self._make_hls_session()
        streamer = self._streamer(hls_session)

        await self._call(streamer=streamer, relay=relay)
        self.assertEqual(relay.active_count, 1)

        with patch("api.vod.vod_preview_source_relay", relay):
            hls_session.on_close()  # what the streamer calls when the session dies
        self.assertEqual(relay.active_count, 0)
        self.assertEqual(preview_budget.active, 0)


class VodPreviewDurationTests(_AccountDbTest):
    """The scrub bar must be full-length from the first frame, so duration
    comes from the provider rather than from what FFmpeg has produced."""

    async def _call(self, kind="movie", item_id="42", client=None, **kwargs):
        with (
            patch("api.vod._get_client", AsyncMock(return_value=client or MagicMock())),
            patch(
                "api.vod.resolve_account_password_with_migration",
                AsyncMock(return_value=SECRET_PASSWORD),
            ),
        ):
            async with self.session_factory() as session:
                return await vod_preview_duration(
                    account_id=self.account_id,
                    request=kwargs.pop("request", _make_request()),
                    kind=kind,
                    item_id=item_id,
                    series_id=kwargs.pop("series_id", None),
                    container_extension=kwargs.pop("container_extension", "mkv"),
                    _auth=_make_auth(),
                    session=session,
                )

    def _client(self, vod_info=None, series_info=None):
        client = MagicMock()
        client.get_vod_info = AsyncMock(return_value=vod_info)
        client.get_series_info = AsyncMock(return_value=series_info)
        client.close = AsyncMock()
        return client

    async def test_movie_duration_from_provider_metadata(self):
        result = await self._call(client=self._client(vod_info={"info": {"duration_secs": 7245}}))
        self.assertEqual(result, {"duration": 7245.0})

    async def test_movie_duration_from_a_clock_string(self):
        result = await self._call(client=self._client(vod_info={"info": {"duration": "01:52:30"}}))
        self.assertEqual(result, {"duration": 6750.0})

    async def test_episode_duration_from_the_series_listing(self):
        series_info = {
            "episodes": {
                "1": [{"id": "906", "info": {"duration_secs": 1500}},
                      {"id": "907", "info": {"duration_secs": 2700}}],
            }
        }
        result = await self._call(
            kind="episode", item_id="907", series_id="88",
            client=self._client(series_info=series_info),
        )
        self.assertEqual(result, {"duration": 2700.0})

    async def test_episode_without_a_series_id_400(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._call(kind="episode", item_id="907")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_the_fallback_probe_spends_a_preview_slot(self):
        """The probe opens a provider connection, so it comes out of the same
        budget rather than sneaking past it."""
        seen = []
        probe = AsyncMock(side_effect=lambda _url: seen.append(preview_budget.active))
        with (
            patch("api.vod.hls_streamer.probe_duration", probe),
            patch("api.vod.vod_preview_source_relay", VodPreviewSourceRelay()),
        ):
            await self._call(client=self._client(vod_info={"info": {}}))
        self.assertEqual(seen, [1])
        self.assertEqual(preview_budget.active, 0, "The probe leaked its slot.")

    async def test_falls_back_to_probing_over_the_loopback_relay(self):
        """When the provider has no duration, probe — but the probe must see
        the relay, not the credentialed URL."""
        probe = AsyncMock(return_value=5400.0)
        relay = VodPreviewSourceRelay()
        with (
            patch("api.vod.hls_streamer.probe_duration", probe),
            patch("api.vod.vod_preview_source_relay", relay),
        ):
            result = await self._call(client=self._client(vod_info={"info": {}}))

        self.assertEqual(result, {"duration": 5400.0})
        probed_url = probe.await_args.args[0]
        self.assertTrue(probed_url.startswith("http://127.0.0.1:"), probed_url)
        self.assertNotIn(SECRET_PASSWORD, probed_url)
        self.assertEqual(relay.active_count, 0, "The probe's token outlived the probe.")

    async def test_provider_metadata_failure_does_not_fail_the_preview(self):
        client = self._client()
        client.get_vod_info = AsyncMock(side_effect=Exception("provider down"))
        with (
            patch("api.vod.hls_streamer.probe_duration", AsyncMock(return_value=None)),
            patch("api.vod.vod_preview_source_relay", VodPreviewSourceRelay()),
        ):
            result = await self._call(client=client)
        self.assertEqual(result, {"duration": None})


class DurationParsingTests(unittest.TestCase):
    def test_numeric_and_clock_forms(self):
        self.assertEqual(vod_api._parse_duration(90), 90.0)
        self.assertEqual(vod_api._parse_duration("90"), 90.0)
        self.assertEqual(vod_api._parse_duration("01:30"), 90.0)
        self.assertEqual(vod_api._parse_duration("1:00:00"), 3600.0)

    def test_unusable_forms_are_none(self):
        for raw in [None, "", "  ", 0, "0", "not a duration", "1:2:3:4"]:
            self.assertIsNone(vod_api._parse_duration(raw), raw)


if __name__ == "__main__":
    unittest.main()
