from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import logging

from auth import require_admin_or_download_user, AuthContext
from database import get_session
from models import XtreamAccount
from services.epg_service import epg_service, NoCatchupSupportError
from services.account_credentials import resolve_account_password_with_migration
from services.xtream_client import XtreamClient


router = APIRouter()
logger = logging.getLogger(__name__)


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
    _admin: None = Depends(require_admin_or_download_user),
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

            # Add archive duration info
            for ch in channels:
                ch["tv_archive_duration"] = epg_service.archive_days_for_channel(ch)

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
            session, account_id, channel_id, actual_days
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
