import asyncio
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func

from auth import require_admin, require_admin_or_download_user, AuthContext
from database import get_session
from models import XtreamAccount, EPGProgram, AppSettings
from services.epg_service import epg_service
from services.epg_ingest_manager import epg_ingest_manager


router = APIRouter()
logger = logging.getLogger(__name__)


def _log_task_result(task: asyncio.Task):
    try:
        task.result()
    except Exception as exc:
        logger.exception("EPG refresh task failed: %s", exc)


class EPGRefreshRequest(BaseModel):
    force: bool = False


def _build_like_pattern(q: str) -> str:
    # Escape backslash first, then LIKE wildcards, so added backslashes aren't re-escaped.
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/epg/search")
async def search_epg(
    account_id: int = Query(..., ge=1),
    q: str = Query(..., min_length=2),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: None = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    pattern = _build_like_pattern(q.lower())
    now_utc = datetime.now(timezone.utc)
    offset_hours = int(account.guide_offset_hours or 0)
    settings_result = await session.execute(select(AppSettings))
    db_settings = settings_result.scalar_one_or_none()
    global_offset_minutes = int(getattr(db_settings, "epg_offset_minutes", 0) or 0)
    threshold = now_utc - timedelta(hours=offset_hours) - timedelta(minutes=global_offset_minutes)
    result = await session.execute(
        select(EPGProgram)
        .where(
            EPGProgram.account_id == account_id,
            or_(
                func.lower(EPGProgram.title).like(pattern, escape="\\"),
                func.lower(EPGProgram.description).like(pattern, escape="\\"),
            ),
            or_(
                EPGProgram.end_time > threshold,
                EPGProgram.has_archive == True,
            ),
        )
        .order_by(EPGProgram.start_time.desc())
        .limit(limit)
        .offset(offset)
    )
    programs = result.scalars().all()
    return [epg_service.serialize_program(row, account, global_offset_minutes) for row in programs]


@router.get("/epg/status")
async def epg_status(_admin: None = Depends(require_admin_or_download_user)):
    return epg_ingest_manager.get_status()


@router.post("/epg/refresh")
async def trigger_epg_refresh(
    request: EPGRefreshRequest,
    _admin: None = Depends(require_admin),
):
    if not epg_ingest_manager.try_claim_refresh():
        raise HTTPException(status_code=409, detail="EPG refresh is already running")

    refresh_task = asyncio.create_task(
        epg_ingest_manager.refresh_all_accounts(force=request.force)
    )
    epg_ingest_manager._pending_task = refresh_task
    refresh_task.add_done_callback(_log_task_result)
    refresh_task.add_done_callback(epg_ingest_manager.release_pending)

    return {
        "status": "started",
        "force": request.force,
    }
