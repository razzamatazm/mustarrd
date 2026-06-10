import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from auth import require_admin_or_download_user, AuthContext
from database import get_session
from models import StarredChannel, XtreamAccount
from services.download_builder import _normalize_provider_start_token
from services.epg_service import epg_service, NoCatchupSupportError
from services.account_credentials import resolve_account_password_with_migration
from services.xtream_client import XtreamClient


router = APIRouter()
logger = logging.getLogger(__name__)

# Stream-preview proxy limits: previews relay provider bytes through the
# backend so credentials embedded in provider URLs never reach the browser.
PREVIEW_MAX_CONCURRENT = 2
PREVIEW_MAX_SECONDS = 300
PREVIEW_CHUNK_SIZE = 64 * 1024
PREVIEW_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)

_active_preview_count = 0

# Strong refs to in-flight connection-close tasks: the event loop only keeps
# weak references, so an unreferenced task could be garbage-collected mid-close.
_pending_preview_closes: set = set()


def _release_preview_slot() -> None:
    global _active_preview_count
    _active_preview_count = max(0, _active_preview_count - 1)


async def _close_preview_connection(provider_response, http_session) -> None:
    """Close the provider response and its session, tolerating partial setup."""
    try:
        if provider_response is not None:
            provider_response.close()
    finally:
        if http_session is not None and not http_session.closed:
            await http_session.close()


def _channel_has_tv_archive(ch: dict) -> bool:
    return int(ch.get("tv_archive", 0) or 0) == 1


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
    global _active_preview_count

    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    password = await resolve_account_password_with_migration(session, account)
    client = XtreamClient(account.server_url, account.username, password)

    if mode == "catchup":
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
        stream_url = client.build_timeshift_url(
            channel_id,
            start_utc,
            duration_minutes,
            provider_start=normalized_start,
        )
    else:
        stream_url = client.build_stream_url(channel_id, "ts")

    if _active_preview_count >= PREVIEW_MAX_CONCURRENT:
        raise HTTPException(
            status_code=429,
            detail="Preview limit reached. Close another preview and try again.",
        )
    _active_preview_count += 1

    http_session = None
    provider_response = None
    try:
        http_session = aiohttp.ClientSession(timeout=PREVIEW_TIMEOUT)
        provider_response = await http_session.get(stream_url)
        if provider_response.status != 200:
            # Never echo the provider URL: it carries credentials.
            raise HTTPException(
                status_code=502,
                detail=f"Provider refused the preview stream (HTTP {provider_response.status})",
            )
    except HTTPException:
        await _close_preview_connection(provider_response, http_session)
        _release_preview_slot()
        raise
    except Exception:
        await _close_preview_connection(provider_response, http_session)
        _release_preview_slot()
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
        _release_preview_slot()
        logger.info(
            "Preview slot released account_id=%s channel_id=%s active=%s",
            account_id, channel_id, _active_preview_count,
        )
        close_task = asyncio.ensure_future(
            _close_preview_connection(provider_response, http_session)
        )
        _pending_preview_closes.add(close_task)
        close_task.add_done_callback(_pending_preview_closes.discard)

    # Failsafe: if the consumer stops reading without disconnecting, the relay
    # generator can park on a yield forever, never reaching its finally — the
    # slot and provider connection would leak until restart. Force cleanup
    # shortly after the relay deadline regardless of consumer behavior.
    failsafe = asyncio.get_running_loop().call_later(
        PREVIEW_MAX_SECONDS + 30,
        lambda: asyncio.ensure_future(cleanup()),
    )
    logger.info(
        "Preview slot acquired account_id=%s channel_id=%s mode=%s active=%s",
        account_id, channel_id, mode, _active_preview_count,
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
