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
import sys
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
from api.channels import preview_channel_stream
from auth import AuthContext, require_admin_or_download_user
from database import Base
from models import XtreamAccount

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
        channels_api._active_preview_count = 0
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
        channels_api._active_preview_count = 0
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
        self.assertEqual(channels_api._active_preview_count, 0)

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
        self.assertEqual(channels_api._active_preview_count, 0)

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
        self.assertEqual(channels_api._active_preview_count, 1)

        # Starlette calls aclose() on the body iterator when the client goes away
        await result.body_iterator.aclose()
        await asyncio.sleep(0)  # the connection close runs as a detached task

        response.close.assert_called_once()
        http_session.close.assert_awaited()
        self.assertEqual(
            channels_api._active_preview_count,
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
        self.assertEqual(channels_api._active_preview_count, 0)

    # ------------------------------------------------------------------
    # concurrency cap
    # ------------------------------------------------------------------

    async def test_preview_limit_returns_429(self):
        """When the concurrent-preview cap is reached, new previews get HTTP 429."""
        channels_api._active_preview_count = channels_api.PREVIEW_MAX_CONCURRENT

        response = _make_provider_response()
        http_session = _make_http_session(response)
        with self.assertRaises(HTTPException) as ctx:
            await self._call_preview(http_session)

        self.assertEqual(ctx.exception.status_code, 429)
        http_session.get.assert_not_awaited()
        self.assertEqual(
            channels_api._active_preview_count,
            channels_api.PREVIEW_MAX_CONCURRENT,
            "A rejected preview must not consume or release a slot.",
        )

    async def test_slot_freed_after_close_allows_new_preview(self):
        """Closing an active preview frees its slot for the next request."""
        channels_api._active_preview_count = channels_api.PREVIEW_MAX_CONCURRENT - 1

        first_response = _make_provider_response()
        first_session = _make_http_session(first_response)
        first = await self._call_preview(first_session)
        self.assertEqual(channels_api._active_preview_count, channels_api.PREVIEW_MAX_CONCURRENT)

        await first.body_iterator.__anext__()
        await first.body_iterator.aclose()
        self.assertEqual(
            channels_api._active_preview_count,
            channels_api.PREVIEW_MAX_CONCURRENT - 1,
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
        self.assertEqual(channels_api._active_preview_count, 1)

        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await asyncio.sleep(0)  # let the detached close task run

        self.assertEqual(
            channels_api._active_preview_count,
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
            self.assertEqual(channels_api._active_preview_count, 1)
            # Read one chunk, then never touch the generator again (no aclose):
            # simulates a client that vanished without a detectable disconnect.
            await result.body_iterator.__anext__()

            await asyncio.sleep(0.4)

        self.assertEqual(channels_api._active_preview_count, 0)
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
        self.assertEqual(channels_api._active_preview_count, 0)

    async def test_cleanup_is_idempotent(self):
        """Generator finally plus background task must not double-release the slot."""
        channels_api._active_preview_count = 1  # an unrelated active preview

        response = _make_provider_response(chunks=(b"a",))
        http_session = _make_http_session(response)

        result = await self._call_preview(http_session)
        async for _chunk in result.body_iterator:
            pass
        await result.background()  # Starlette always runs background afterwards

        self.assertEqual(
            channels_api._active_preview_count,
            1,
            "Cleanup ran twice: the unrelated preview's slot was stolen.",
        )


if __name__ == "__main__":
    unittest.main()
