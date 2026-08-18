import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from auth import require_admin_or_download_user, AuthContext
from database import get_session
from models import StarredChannel, XtreamAccount
from services.download_builder import _normalize_provider_start_token
from api.hls_common import (
    acquire_preview_slot,
    close_provider_connection,
    detach_cleanup,
    hls_asset_response,
    hls_http_error,
    hls_playlist_response,
    release_preview_slot,
)
from services.epg_service import epg_service, NoCatchupSupportError
from services.hls_streamer import (
    HLS_ASSET_PATTERN,
    HLSError,
    HLSStartError,
    hls_streamer,
    preview_session_key,
)
from services.account_credentials import resolve_account_password_with_migration
from services.logo_cache import logo_cache
from services.preview_budget import PREVIEW_MAX_CONCURRENT, preview_budget
from services.xtream_client import XtreamClient


router = APIRouter()
logger = logging.getLogger(__name__)

# Stream-preview proxy limits: previews relay provider bytes through the
# backend so credentials embedded in provider URLs never reach the browser.
# The concurrency budget is shared with VOD previews; see services/preview_budget.
PREVIEW_MAX_SECONDS = 300
PREVIEW_CHUNK_SIZE = 64 * 1024
PREVIEW_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)

def _channel_has_tv_archive(ch: dict) -> bool:
    return int(ch.get("tv_archive", 0) or 0) == 1


async def _resolve_preview_stream_url(
    session: AsyncSession,
    account_id: int,
    channel_id: str,
    mode: str,
    start_timestamp: Optional[int],
    stop_timestamp: Optional[int],
    provider_start: Optional[str],
) -> str:
    """Build the provider URL for a preview. The result carries the account
    username and password, so it must never reach a response or a process
    argument list."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    password = await resolve_account_password_with_migration(session, account)
    client = XtreamClient(account.server_url, account.username, password)

    if mode != "catchup":
        return client.build_stream_url(channel_id, "ts")

    if not start_timestamp or not stop_timestamp or stop_timestamp <= start_timestamp:
        raise HTTPException(
            status_code=400,
            detail="Catchup preview requires valid start and stop timestamps",
        )
    try:
        start_utc = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid start timestamp")
    duration_minutes = max(1, (stop_timestamp - start_timestamp) // 60)
    normalized_start = (
        _normalize_provider_start_token(provider_start, 0) if provider_start else None
    )
    return client.build_timeshift_url(
        channel_id,
        start_utc,
        duration_minutes,
        provider_start=normalized_start,
    )


def _preview_fingerprint(
    mode: str,
    start_timestamp: Optional[int],
    stop_timestamp: Optional[int],
    provider_start: Optional[str],
) -> str:
    """Identify *what* a preview session is showing, so a request for a
    different program on the same channel rebuilds it. Derived only from
    request parameters, never from the credentialed URL."""
    return f"{mode}:{start_timestamp}:{stop_timestamp}:{provider_start}"


def _provider_refused(status: int) -> str:
    # Never echo the provider URL: it carries credentials.
    return f"Provider refused the preview stream (HTTP {status})"


class _ProviderStream:
    """A provider URL presented to HLSStreamer as a plain byte source.

    The backend owns the HTTP connection, so FFmpeg is handed bytes on stdin
    and never sees the credentialed URL — which `ps` would otherwise expose
    to every user on the host.
    """

    def __init__(self, url: str):
        self._url = url
        self._http_session = None
        self._response = None

    async def open(self):
        self._http_session = aiohttp.ClientSession(timeout=PREVIEW_TIMEOUT)
        self._response = await self._http_session.get(self._url)
        if self._response.status != 200:
            status = self._response.status
            await self.close()
            raise HLSStartError(_provider_refused(status))
        return self._response.content.iter_chunked(PREVIEW_CHUNK_SIZE)

    async def close(self) -> None:
        response, self._response = self._response, None
        http_session, self._http_session = self._http_session, None
        await close_provider_connection(response, http_session)


@router.get("/logos")
async def get_channel_logo(
    url: str = Query(..., min_length=1, max_length=2048),
    _auth: AuthContext = Depends(require_admin_or_download_user),
):
    """Serve a provider channel logo through the disk cache.

    Provider logo hosts rarely send cache headers, so the browser refetches
    every logo on every guide render. Fetch once, cache to disk, and serve
    with a long max-age so repeat visits never hit the network.
    """
    result = await logo_cache.get_logo(url)
    if result is None:
        raise HTTPException(status_code=404, detail="Logo unavailable")

    content, content_type = result
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
            # Logo bytes come from third-party hosts; the sandbox stops a
            # crafted SVG from running scripts on our origin if opened directly.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/accounts/{account_id}/categories")
async def get_categories(
    account_id: int,
    _admin: None = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get channel categories for an account."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        categories = await epg_service.get_categories(session, account_id)
        return categories
    except Exception:
        logger.exception("Failed to load channel categories account_id=%s", account_id)
        raise HTTPException(status_code=500, detail="Failed to load channel categories")


@router.get("/accounts/{account_id}/channels")
async def get_channels(
    account_id: int,
    category_id: Optional[str] = Query(None),
    catchup_only: bool = Query(True, description="Only show channels with catchup/timeshift support"),
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get channels for an account, optionally filtered by category."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        starred_result = await session.execute(
            select(StarredChannel.channel_id).where(
                StarredChannel.user_id == auth.user_id,
                StarredChannel.account_id == account_id,
            )
        )
        starred_ids = set(starred_result.scalars().all())

        password = await resolve_account_password_with_migration(session, account)
        client = XtreamClient(account.server_url, account.username, password)
        try:
            channels = await client.get_live_streams(category_id)

            if catchup_only:
                # Filter to only channels with catchup enabled
                channels = [
                    ch for ch in channels
                    if _channel_has_tv_archive(ch) and epg_service.archive_days_for_channel(ch) > 0
                ]

            # Add archive duration info and the per-user starred flag
            for ch in channels:
                ch["tv_archive_duration"] = epg_service.archive_days_for_channel(ch)
                ch["starred"] = str(ch.get("stream_id")) in starred_ids

            # Starred channels float to the top; provider order is kept otherwise
            channels.sort(key=lambda ch: not ch["starred"])

            return channels
        finally:
            await client.close()

    except Exception:
        logger.exception(
            "Failed to load channels account_id=%s category_id=%s",
            account_id,
            category_id,
        )
        raise HTTPException(status_code=400, detail="Failed to load channels from provider")


@router.post("/accounts/{account_id}/channels/{channel_id}/star")
async def toggle_channel_star(
    account_id: int,
    channel_id: str,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Toggle the requesting user's star on a channel."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    channel_key = str(channel_id)
    existing_result = await session.execute(
        select(StarredChannel).where(
            StarredChannel.user_id == auth.user_id,
            StarredChannel.account_id == account_id,
            StarredChannel.channel_id == channel_key,
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing:
        await session.delete(existing)
        await session.commit()
        return {"starred": False}

    session.add(
        StarredChannel(
            user_id=auth.user_id,
            account_id=account_id,
            channel_id=channel_key,
        )
    )
    await session.commit()
    return {"starred": True}


@router.get("/accounts/{account_id}/channels/{channel_id}/epg")
async def get_channel_epg(
    account_id: int,
    channel_id: str,
    days_back: int = Query(7, ge=1, le=365),
    fresh: bool = Query(False, description="Fetch live channel EPG before falling back to stored guide data"),
    _admin: None = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get EPG data for a specific channel."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        channel_archive_days = await epg_service.get_channel_archive_days(session, account_id, channel_id)
        actual_days = min(days_back, channel_archive_days)
        if actual_days <= 0:
            return []
        epg_data = await epg_service.get_epg_for_channel(
            session,
            account_id,
            channel_id,
            prefer_live=fresh,
            days_back=actual_days,
            # Already resolved above; passing it through avoids fetching the
            # provider's entire channel list a second time in the same request.
            archive_days=channel_archive_days,
        )
        return epg_data
    except NoCatchupSupportError:
        return []
    except Exception:
        logger.exception(
            "Failed to load channel EPG account_id=%s channel_id=%s",
            account_id,
            channel_id,
        )
        raise HTTPException(status_code=500, detail="Failed to load channel guide data")


@router.get("/accounts/{account_id}/channels/{channel_id}/catchup")
async def get_catchup_programs(
    account_id: int,
    channel_id: str,
    days_back: int = Query(7, ge=1, le=365),
    _admin: None = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get past programs available for catchup/timeshift."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        channel_archive_days = await epg_service.get_channel_archive_days(session, account_id, channel_id)
        actual_days = min(days_back, channel_archive_days)
        if actual_days <= 0:
            return []
        programs = await epg_service.get_past_programs(
            session, account_id, channel_id, actual_days,
            # Already resolved above; passing it through avoids fetching the
            # provider's entire channel list a second time in the same request.
            archive_days=channel_archive_days,
        )
        return programs
    except NoCatchupSupportError:
        return []
    except Exception:
        logger.exception(
            "Failed to load catchup programs account_id=%s channel_id=%s",
            account_id,
            channel_id,
        )
        raise HTTPException(status_code=500, detail="Failed to load catchup programs")


@router.get("/accounts/{account_id}/channels/{channel_id}/preview")
async def preview_channel_stream(
    account_id: int,
    channel_id: str,
    mode: str = Query("live", pattern="^(live|catchup)$"),
    start_timestamp: Optional[int] = Query(None, ge=0),
    stop_timestamp: Optional[int] = Query(None, ge=0),
    provider_start: Optional[str] = Query(None, max_length=64),
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Relay a short-lived preview of a live or catchup stream.

    The provider URL embeds account credentials, so it never leaves the
    backend: the stream is opened server-side and bytes are proxied to the
    authenticated browser session.
    """
    stream_url = await _resolve_preview_stream_url(
        session, account_id, channel_id, mode, start_timestamp, stop_timestamp, provider_start
    )

    acquire_preview_slot()

    http_session = None
    provider_response = None
    try:
        http_session = aiohttp.ClientSession(timeout=PREVIEW_TIMEOUT)
        provider_response = await http_session.get(stream_url)
        if provider_response.status != 200:
            raise HTTPException(
                status_code=502,
                detail=_provider_refused(provider_response.status),
            )
    except HTTPException:
        await close_provider_connection(provider_response, http_session)
        release_preview_slot()
        raise
    except Exception:
        await close_provider_connection(provider_response, http_session)
        release_preview_slot()
        logger.exception(
            "Preview stream failed account_id=%s channel_id=%s mode=%s",
            account_id,
            channel_id,
            mode,
        )
        raise HTTPException(status_code=502, detail="Could not connect to the provider for preview")

    cleanup_done = False

    async def cleanup():
        # Idempotent: invoked from the generator's finally, the response
        # background task, and the failsafe timer; whichever runs first wins.
        #
        # CRITICAL: no awaits in this function. On client disconnect Starlette
        # CANCELS the streaming task, so this runs with a CancelledError
        # pending — the first await would re-raise it and skip everything
        # after, leaking the slot until restart (while cleanup_done, already
        # set, blocks the failsafe from retrying). Cancellation can only land
        # at await points, so an await-free body always runs to completion;
        # the connection close is detached into its own task.
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        failsafe.cancel()
        release_preview_slot()
        logger.info(
            "Preview slot released account_id=%s channel_id=%s active=%s",
            account_id, channel_id, preview_budget.active,
        )
        detach_cleanup(close_provider_connection(provider_response, http_session))

    # Failsafe: if the consumer stops reading without disconnecting, the relay
    # generator can park on a yield forever, never reaching its finally — the
    # slot and provider connection would leak until restart. Force cleanup
    # shortly after the relay deadline regardless of consumer behavior.
    def _spawn_failsafe_cleanup():
        detach_cleanup(cleanup())

    failsafe = asyncio.get_running_loop().call_later(
        PREVIEW_MAX_SECONDS + 30,
        _spawn_failsafe_cleanup,
    )
    logger.info(
        "Preview slot acquired account_id=%s channel_id=%s mode=%s active=%s",
        account_id, channel_id, mode, preview_budget.active,
    )

    async def relay():
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PREVIEW_MAX_SECONDS
            async for chunk in provider_response.content.iter_chunked(PREVIEW_CHUNK_SIZE):
                yield chunk
                if loop.time() >= deadline:
                    break
        finally:
            # Runs on normal completion, the deadline break, and client
            # disconnect (generator aclose), so the provider connection
            # never outlives the preview.
            await cleanup()

    return StreamingResponse(
        relay(),
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
        # Safety net for a disconnect before the first byte is sent: closing
        # a never-started generator skips its finally block, but Starlette
        # still runs the background task after the response ends.
        background=BackgroundTask(cleanup),
    )


@router.get("/accounts/{account_id}/channels/{channel_id}/preview/hls/{asset}")
async def preview_channel_hls_asset(
    account_id: int,
    channel_id: str,
    asset: str,
    mode: str = Query("live", pattern="^(live|catchup)$"),
    start_timestamp: Optional[int] = Query(None, ge=0),
    stop_timestamp: Optional[int] = Query(None, ge=0),
    provider_start: Optional[str] = Query(None, max_length=64),
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve a Converted preview: the provider stream repackaged by FFmpeg.

    Requesting playlist.m3u8 starts (or reuses) a repackaging session that
    copies the video and re-encodes the audio to AAC-LC, for browsers that
    cannot decode the provider's original stream. Segments are relative to
    the playlist, so they carry no query string and resolve by channel alone.

    A Converted preview is still a preview: it takes a slot from the same
    budget as the Direct path and stops at the same time cap.
    """
    if not HLS_ASSET_PATTERN.match(asset):
        raise HTTPException(status_code=404, detail="Unknown stream asset")

    key = preview_session_key(account_id, channel_id)

    if asset != "playlist.m3u8":
        hls_session = hls_streamer.get_active(key)
        if not hls_session:
            raise HTTPException(status_code=409, detail="No active preview session for this channel")
        hls_streamer.touch(hls_session)
        return hls_asset_response(hls_session, asset)

    stream_url = await _resolve_preview_stream_url(
        session, account_id, channel_id, mode, start_timestamp, stop_timestamp, provider_start
    )
    fingerprint = _preview_fingerprint(mode, start_timestamp, stop_timestamp, provider_start)

    existing = hls_streamer.get_active(key)
    if existing is not None and existing.fingerprint == fingerprint:
        hls_streamer.touch(existing)
        return hls_playlist_response(existing)

    acquire_preview_slot()
    released = False

    def release_slot():
        # One-shot: the streamer calls this when the session dies, and this
        # handler calls it when the session never came to life.
        nonlocal released
        if released:
            return
        released = True
        release_preview_slot()
        logger.info(
            "Converted preview slot released account_id=%s channel_id=%s active=%s",
            account_id, channel_id, preview_budget.active,
        )

    hls_session = None

    async def abandon():
        # wait_for_playlist can fail on a session that is still running, so
        # tear it down rather than just dropping the slot: otherwise FFmpeg
        # would keep the stream open with nothing accounting for it.
        # Release first — under cancellation the await below never returns.
        release_slot()
        if hls_session is not None:
            await hls_streamer.close_session(hls_session)

    try:
        hls_session = await hls_streamer.get_or_create_stream(
            key,
            _ProviderStream(stream_url),
            fingerprint,
            PREVIEW_MAX_SECONDS,
            on_close=release_slot,
        )
        await hls_streamer.wait_for_playlist(hls_session)
    except HLSError as exc:
        await abandon()
        raise hls_http_error(exc)
    except BaseException:
        await abandon()
        raise

    logger.info(
        "Converted preview started account_id=%s channel_id=%s mode=%s active=%s",
        account_id, channel_id, mode, preview_budget.active,
    )
    return hls_playlist_response(hls_session)


@router.get("/accounts/{account_id}/channels/{channel_id}")
async def get_channel_info(
    account_id: int,
    channel_id: str,
    _admin: None = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get info for a specific channel."""
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        password = await resolve_account_password_with_migration(session, account)
        client = XtreamClient(account.server_url, account.username, password)
        try:
            channels = await client.get_live_streams()
            channel = next((ch for ch in channels if str(ch.get("stream_id")) == channel_id), None)

            if not channel:
                raise HTTPException(status_code=404, detail="Channel not found")

            return channel
        finally:
            await client.close()

    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "Failed to load channel info account_id=%s channel_id=%s",
            account_id,
            channel_id,
        )
        raise HTTPException(status_code=400, detail="Failed to load channel from provider")
