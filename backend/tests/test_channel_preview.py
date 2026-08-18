"""
Tests for the Browse EPG stream-preview proxy endpoint.

GET /accounts/{account_id}/channels/{channel_id}/preview relays a live or
catchup stream through the backend so the provider URL — which embeds the
account username and password — never reaches the browser. The endpoint must:

- require authentication (admin or download_only),
- never leak credentials in any response field (error detail, headers),
- close the provider connection when the client disconnects,
- enforce the concurrent-preview cap with HTTP 429.
"""
import asyncio
import inspect
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import api.channels as channels_api
from services.preview_budget import preview_budget
from api.channels import preview_channel_hls_asset, preview_channel_stream
from auth import AuthContext, require_admin_or_download_user
from database import Base
from models import XtreamAccount
from services.hls_streamer import HLSSession, HLSStartError, HLSUnavailableError

SECRET_PASSWORD = "sup3r-secret-pw"
SECRET_USERNAME = "secret-user"
SERVER_URL = "http://provider.example:8080"


def _make_account():
    return XtreamAccount(
        name="Provider",
        server_url=SERVER_URL,
        username=SECRET_USERNAME,
        password="",
        password_encrypted="enc",
    )


def _make_auth():
    return AuthContext(authenticated=True, role="download_only", user_id=1, is_admin=False)


def _make_provider_response(status=200, chunks=(b"\x47" * 188,)):
    response = MagicMock()
    response.status = status
    response.close = MagicMock()

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


def _make_http_session(response):
    session = MagicMock()
    session.get = AsyncMock(return_value=response)
    session.closed = False
    session.close = AsyncMock()
    return session


class ChannelPreviewTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        preview_budget.active = 0
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_factory() as session:
            account = _make_account()
            session.add(account)
            await session.commit()
            await session.refresh(account)
            self.account_id = account.id

    async def asyncTearDown(self):
        preview_budget.active = 0
        await self.engine.dispose()

    async def _call_preview(self, http_session, mode="live", **kwargs):
        with (
            patch("api.channels.aiohttp.ClientSession", return_value=http_session),
            patch(
                "api.channels.resolve_account_password_with_migration",
                AsyncMock(return_value=SECRET_PASSWORD),
            ),
        ):
            async with self.session_factory() as session:
                return await preview_channel_stream(
                    account_id=kwargs.pop("account_id", self.account_id),
                    channel_id=kwargs.pop("channel_id", "55"),
                    mode=mode,
                    start_timestamp=kwargs.pop("start_timestamp", None),
                    stop_timestamp=kwargs.pop("stop_timestamp", None),
                    provider_start=kwargs.pop("provider_start", None),
                    _auth=_make_auth(),
                    session=session,
                )

    # ------------------------------------------------------------------
    # auth wiring
    # ------------------------------------------------------------------

    def test_preview_requires_authentication(self):
        """preview_channel_stream must declare require_admin_or_download_user."""
        sig = inspect.signature(preview_channel_stream)
        auth_param = sig.parameters.get("_auth")
        self.assertIsNotNone(auth_param, "_auth parameter missing from handler")
        self.assertEqual(auth_param.default.dependency, require_admin_or_download_user)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    async def test_unknown_account_404(self):
        response = _make_provider_response()
        with self.assertRaises(HTTPException) as ctx:
            await self._call_preview(_make_http_session(response), account_id=9999)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_catchup_without_timestamps_400(self):
        response = _make_provider_response()
        with self.assertRaises(HTTPException) as ctx:
            await self._call_preview(_make_http_session(response), mode="catchup")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(preview_budget.active, 0)

    async def test_catchup_builds_timeshift_url(self):
        """Catchup mode must hit the provider's timeshift URL with the program duration."""
        start = int(datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc).timestamp())
        stop = start + 3600
        response = _make_provider_response()
        http_session = _make_http_session(response)

        result = await self._call_preview(
            http_session, mode="catchup", start_timestamp=start, stop_timestamp=stop
        )
        try:
            requested_url = http_session.get.call_args[0][0]
            self.assertIn("/timeshift/", requested_url)
            self.assertIn("/60/", requested_url, "Duration must be 60 minutes for a 1-hour program.")
            self.assertIn("2026-06-08:20-00", requested_url)
        finally:
            await result.body_iterator.aclose()

    async def test_live_mode_builds_live_url(self):
        response = _make_provider_response()
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session, mode="live")
        try:
            requested_url = http_session.get.call_args[0][0]
            self.assertIn("/live/", requested_url)
            self.assertTrue(requested_url.endswith("/55.ts"))
        finally:
            await result.body_iterator.aclose()

    # ------------------------------------------------------------------
    # credential leakage
    # ------------------------------------------------------------------

    async def test_provider_error_detail_has_no_credentials(self):
        """A provider failure must not echo the URL, username, or password."""
        response = _make_provider_response(status=404)
        http_session = _make_http_session(response)

        with self.assertRaises(HTTPException) as ctx:
            await self._call_preview(http_session)

        self.assertEqual(ctx.exception.status_code, 502)
        detail = str(ctx.exception.detail)
        self.assertNotIn(SECRET_PASSWORD, detail)
        self.assertNotIn(SECRET_USERNAME, detail)
        self.assertNotIn("provider.example", detail)
        # Provider connection must be torn down and the slot released
        response.close.assert_called_once()
        http_session.close.assert_awaited()
        self.assertEqual(preview_budget.active, 0)

    async def test_success_response_has_no_credentials(self):
        """Headers of a successful preview must not carry the provider URL."""
        response = _make_provider_response()
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        try:
            self.assertEqual(result.media_type, "video/mp2t")
            for name, value in result.headers.items():
                self.assertNotIn(SECRET_PASSWORD, value, f"Credential leaked in header {name}")
                self.assertNotIn(SECRET_USERNAME, value, f"Credential leaked in header {name}")
                self.assertNotIn("provider.example", value, f"Provider URL leaked in header {name}")
        finally:
            await result.body_iterator.aclose()

    # ------------------------------------------------------------------
    # disconnect handling
    # ------------------------------------------------------------------

    async def test_client_disconnect_closes_provider_connection(self):
        """Closing the response generator (client disconnect) must close the provider stream."""
        chunk = b"\x47" * 188
        response = _make_provider_response(chunks=(chunk, chunk, chunk))
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        first = await result.body_iterator.__anext__()
        self.assertEqual(first, chunk)
        self.assertEqual(preview_budget.active, 1)

        # Starlette calls aclose() on the body iterator when the client goes away
        await result.body_iterator.aclose()
        await asyncio.sleep(0)  # the connection close runs as a detached task

        response.close.assert_called_once()
        http_session.close.assert_awaited()
        self.assertEqual(
            preview_budget.active,
            0,
            "Preview slot must be released on client disconnect.",
        )

    async def test_stream_end_releases_slot(self):
        """Draining the stream to its natural end must also release the slot."""
        response = _make_provider_response(chunks=(b"a", b"b"))
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        received = [chunk async for chunk in result.body_iterator]
        await asyncio.sleep(0)  # the connection close runs as a detached task

        self.assertEqual(received, [b"a", b"b"])
        response.close.assert_called_once()
        http_session.close.assert_awaited()
        self.assertEqual(preview_budget.active, 0)

    # ------------------------------------------------------------------
    # concurrency cap
    # ------------------------------------------------------------------

    async def test_preview_limit_returns_429(self):
        """When the concurrent-preview cap is reached, new previews get HTTP 429."""
        preview_budget.active = preview_budget.limit

        response = _make_provider_response()
        http_session = _make_http_session(response)
        with self.assertRaises(HTTPException) as ctx:
            await self._call_preview(http_session)

        self.assertEqual(ctx.exception.status_code, 429)
        http_session.get.assert_not_awaited()
        self.assertEqual(
            preview_budget.active,
            preview_budget.limit,
            "A rejected preview must not consume or release a slot.",
        )

    async def test_slot_freed_after_close_allows_new_preview(self):
        """Closing an active preview frees its slot for the next request."""
        preview_budget.active = preview_budget.limit - 1

        first_response = _make_provider_response()
        first_session = _make_http_session(first_response)
        first = await self._call_preview(first_session)
        self.assertEqual(preview_budget.active, preview_budget.limit)

        await first.body_iterator.__anext__()
        await first.body_iterator.aclose()
        self.assertEqual(
            preview_budget.active,
            preview_budget.limit - 1,
        )

        second_response = _make_provider_response()
        second_session = _make_http_session(second_response)
        second = await self._call_preview(second_session)
        try:
            self.assertEqual(second.media_type, "video/mp2t")
        finally:
            await second.body_iterator.aclose()

    async def test_cancelled_consumer_releases_slot(self):
        """Starlette CANCELS the streaming task on client disconnect. Cleanup
        used to await the connection close first, so the pending CancelledError
        aborted it before the slot release — leaking the slot until restart."""
        async def infinite_chunks(_size):
            while True:
                await asyncio.sleep(0.01)
                yield b"\x47" * 188

        response = _make_provider_response()
        response.content.iter_chunked = infinite_chunks
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)

        async def consume():
            async for _ in result.body_iterator:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        self.assertEqual(preview_budget.active, 1)

        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.sleep(0)  # let the detached close task run

        self.assertEqual(
            preview_budget.active,
            0,
            "Slot must be released even when cleanup runs under cancellation.",
        )
        response.close.assert_called_once()
        http_session.close.assert_awaited()

    async def test_failsafe_releases_parked_relay(self):
        """A consumer that stops reading without disconnecting parks the relay
        generator on a yield forever; the failsafe timer must still release
        the slot and close the provider connection."""
        response = _make_provider_response(chunks=(b"a", b"b", b"c"))
        http_session = _make_http_session(response)

        with patch.object(channels_api, "PREVIEW_MAX_SECONDS", -29.9):
            result = await self._call_preview(http_session)
            self.assertEqual(preview_budget.active, 1)
            # Read one chunk, then never touch the generator again (no aclose):
            # simulates a client that vanished without a detectable disconnect.
            await result.body_iterator.__anext__()

            await asyncio.sleep(0.4)

        self.assertEqual(preview_budget.active, 0)
        response.close.assert_called_once()
        http_session.close.assert_awaited()
        await result.body_iterator.aclose()

    async def test_disconnect_before_first_byte_releases_via_background(self):
        """
        Closing a never-started generator skips its finally block (PEP 525),
        so the response's background task must release the provider connection
        and the preview slot. Starlette runs it after the response ends.
        """
        response = _make_provider_response()
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        # Client goes away before the first chunk: Starlette closes the
        # unstarted body iterator (no-op for cleanup) then runs background.
        await result.body_iterator.aclose()
        self.assertIsNotNone(result.background, "Preview response must carry a cleanup background task.")
        await result.background()
        await asyncio.sleep(0)  # the connection close runs as a detached task

        response.close.assert_called_once()
        http_session.close.assert_awaited()
        self.assertEqual(preview_budget.active, 0)

    async def test_cleanup_is_idempotent(self):
        """Generator finally plus background task must not double-release the slot."""
        preview_budget.active = 1  # an unrelated active preview

        response = _make_provider_response(chunks=(b"a",))
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        async for _chunk in result.body_iterator:
            pass
        await result.background()  # Starlette always runs background afterwards

        self.assertEqual(
            preview_budget.active,
            1,
            "Cleanup ran twice: the unrelated preview's slot was stolen.",
        )


class ConvertedPreviewTests(unittest.IsolatedAsyncioTestCase):
    """
    GET .../preview/hls/{asset} is the Converted preview: the backend
    repackages the provider stream with FFmpeg for browsers that cannot decode
    it directly. It shares the Direct preview's concurrency budget, and the
    credentialed provider URL must reach neither the browser nor FFmpeg's argv.
    """

    async def asyncSetUp(self):
        preview_budget.active = 0
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_factory() as session:
            account = _make_account()
            session.add(account)
            await session.commit()
            await session.refresh(account)
            self.account_id = account.id

    async def asyncTearDown(self):
        preview_budget.active = 0
        await self.engine.dispose()

    def _make_hls_session(self, on_close=None):
        directory = Path(tempfile.mkdtemp(prefix="converted-preview-test-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        session = HLSSession(key="preview:1:55", directory=directory, live=True)
        session.on_close = on_close
        session.playlist_path.write_text("#EXTM3U\nseg00000.m4s\n")
        return session

    async def _call(self, asset="playlist.m3u8", mode="live", streamer=None, **kwargs):
        with (
            patch("api.channels.hls_streamer", streamer or MagicMock()),
            patch(
                "api.channels.resolve_account_password_with_migration",
                AsyncMock(return_value=SECRET_PASSWORD),
            ),
        ):
            async with self.session_factory() as session:
                return await preview_channel_hls_asset(
                    account_id=kwargs.pop("account_id", self.account_id),
                    channel_id=kwargs.pop("channel_id", "55"),
                    asset=asset,
                    mode=mode,
                    start_timestamp=kwargs.pop("start_timestamp", None),
                    stop_timestamp=kwargs.pop("stop_timestamp", None),
                    provider_start=kwargs.pop("provider_start", None),
                    _auth=_make_auth(),
                    session=session,
                )

    def _streamer(self, hls_session, active=None):
        streamer = MagicMock()
        streamer.get_active.return_value = active

        async def start(_key, _source, _fingerprint, _max_seconds, on_close=None):
            hls_session.on_close = on_close
            return hls_session

        streamer.get_or_create_stream = AsyncMock(side_effect=start)
        streamer.wait_for_playlist = AsyncMock()
        streamer.close_session = AsyncMock()
        return streamer

    def test_converted_preview_requires_authentication(self):
        sig = inspect.signature(preview_channel_hls_asset)
        auth_param = sig.parameters.get("_auth")
        self.assertIsNotNone(auth_param, "_auth parameter missing from handler")
        self.assertEqual(auth_param.default.dependency, require_admin_or_download_user)

    async def test_unknown_asset_name_404(self):
        for asset in ["../../etc/passwd", "ffmpeg.log", "probe.ts"]:
            with self.assertRaises(HTTPException) as ctx:
                await self._call(asset=asset)
            self.assertEqual(ctx.exception.status_code, 404, asset)

    async def test_segment_without_session_409(self):
        streamer = self._streamer(None, active=None)
        with self.assertRaises(HTTPException) as ctx:
            await self._call(asset="seg00000.m4s", streamer=streamer)
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_playlist_starts_session_and_takes_a_slot(self):
        hls_session = self._make_hls_session()
        streamer = self._streamer(hls_session)

        response = await self._call(streamer=streamer)

        self.assertEqual(response.media_type, "application/vnd.apple.mpegurl")
        self.assertEqual(preview_budget.active, 1)
        streamer.get_or_create_stream.assert_awaited_once()
        # A Converted preview must be as short-lived as a Direct one.
        self.assertEqual(
            streamer.get_or_create_stream.await_args.args[3], channels_api.PREVIEW_MAX_SECONDS
        )

    async def test_provider_url_is_handed_over_as_a_stream_not_an_argument(self):
        """FFmpeg must be fed bytes, never the credentialed URL: argv is
        visible to every user on the host via `ps`."""
        hls_session = self._make_hls_session()
        streamer = self._streamer(hls_session)

        await self._call(streamer=streamer)

        source = streamer.get_or_create_stream.await_args.args[1]
        self.assertIsInstance(source, channels_api._ProviderStream)
        fingerprint = streamer.get_or_create_stream.await_args.args[2]
        self.assertNotIn(SECRET_PASSWORD, fingerprint)
        self.assertNotIn(SECRET_USERNAME, fingerprint)

    async def test_playlist_response_has_no_credentials(self):
        hls_session = self._make_hls_session()
        response = await self._call(streamer=self._streamer(hls_session))
        for name, value in response.headers.items():
            self.assertNotIn(SECRET_PASSWORD, value, f"Credential leaked in header {name}")
            self.assertNotIn(SECRET_USERNAME, value, f"Credential leaked in header {name}")
            self.assertNotIn("provider.example", value, f"Provider URL leaked in header {name}")

    async def test_shares_the_direct_preview_budget(self):
        """One viewer must not be able to hold a Direct slot and a Converted
        slot at once — unlike a download, a live encode never ends by itself."""
        preview_budget.active = preview_budget.limit
        streamer = self._streamer(self._make_hls_session())

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)

        self.assertEqual(ctx.exception.status_code, 429)
        streamer.get_or_create_stream.assert_not_awaited()
        self.assertEqual(
            preview_budget.active, preview_budget.limit
        )

    async def test_reusing_a_live_session_does_not_take_a_second_slot(self):
        hls_session = self._make_hls_session()
        hls_session.fingerprint = "live:None:None:None"
        streamer = self._streamer(hls_session, active=hls_session)

        await self._call(streamer=streamer)

        streamer.get_or_create_stream.assert_not_awaited()
        self.assertEqual(preview_budget.active, 0)
        streamer.touch.assert_called_once_with(hls_session)

    async def test_losing_the_start_race_gives_the_slot_back(self):
        """Two players opening the same channel at once both reserve a slot,
        but only one session exists. The streamer hands the loser's callback
        straight back, and the handler must not sit on the reservation."""
        hls_session = self._make_hls_session(on_close=lambda: None)
        streamer = self._streamer(hls_session)

        async def reuse(_key, _source, _fingerprint, _max_seconds, on_close=None):
            on_close()  # not adopted: an equivalent session already existed
            return hls_session

        streamer.get_or_create_stream = AsyncMock(side_effect=reuse)

        await self._call(streamer=streamer)

        self.assertEqual(preview_budget.active, 0)

    async def test_failed_start_releases_the_slot_and_tears_down(self):
        streamer = self._streamer(None)
        streamer.get_or_create_stream = AsyncMock(side_effect=HLSStartError("no video track"))

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(
            preview_budget.active, 0, "A failed Converted preview leaked its slot."
        )
        # The streamer already tore down the session it failed to start;
        # there is nothing for the handler to close.
        streamer.close_session.assert_not_awaited()

    async def test_ffmpeg_missing_returns_503(self):
        streamer = self._streamer(None)
        streamer.get_or_create_stream = AsyncMock(
            side_effect=HLSUnavailableError("FFmpeg was not found")
        )
        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(preview_budget.active, 0)

    async def test_playlist_timeout_releases_the_slot(self):
        hls_session = self._make_hls_session()
        streamer = self._streamer(hls_session)
        streamer.wait_for_playlist = AsyncMock(side_effect=HLSStartError("timed out"))

        with self.assertRaises(HTTPException) as ctx:
            await self._call(streamer=streamer)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(preview_budget.active, 0)
        streamer.close_session.assert_awaited_once_with(hls_session)

    async def test_abandon_tears_down_its_own_session_not_the_key(self):
        """A request whose playlist never arrived must not kill the healthy
        replacement session a later request already started on that channel —
        that would 404 someone else's player mid-playback."""
        mine = self._make_hls_session()
        streamer = self._streamer(mine)
        streamer.wait_for_playlist = AsyncMock(side_effect=HLSStartError("timed out"))

        with self.assertRaises(HTTPException):
            await self._call(streamer=streamer)

        # Torn down by identity, so a session that replaced it is untouched.
        streamer.close_session.assert_awaited_once_with(mine)

    async def test_abandon_tears_down_its_own_session_not_the_key(self):
        """A request whose playlist never arrived must not kill the healthy
        replacement session a later request already started on that channel —
        that would 404 someone else's player mid-playback."""
        mine = self._make_hls_session()
        streamer = self._streamer(mine)
        streamer.wait_for_playlist = AsyncMock(side_effect=HLSStartError("timed out"))

        with self.assertRaises(HTTPException):
            await self._call(streamer=streamer)

        # Torn down by identity, so a session that replaced it is untouched.
        streamer.close_session.assert_awaited_once_with(mine)

    async def test_catchup_without_timestamps_400(self):
        streamer = self._streamer(self._make_hls_session())
        with self.assertRaises(HTTPException) as ctx:
            await self._call(mode="catchup", streamer=streamer)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(preview_budget.active, 0)


if __name__ == "__main__":
    unittest.main()
