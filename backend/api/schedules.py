from datetime import datetime, timezone
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, conint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from auth import require_admin_or_download_user, AuthContext
from database import get_session
from models import ScheduledRecording, ScheduledStatus, XtreamAccount, Download
from services.download_manager import download_manager
from services.file_namer import file_namer


router = APIRouter()
logger = logging.getLogger(__name__)


class ScheduleCreate(BaseModel):
    account_id: int
    channel_id: str
    channel_name: str
    program: dict
    custom_filename: Optional[str] = None
    pre_padding_minutes: Optional[conint(ge=0, le=120)] = 0
    post_padding_minutes: Optional[conint(ge=0, le=120)] = 0


def _coerce_ts(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_program(program: dict) -> tuple[datetime, datetime, int, int, int, str | None, str | None]:
    start_timestamp = _coerce_ts(program.get("start_timestamp"))
    stop_timestamp = _coerce_ts(program.get("stop_timestamp"))

    if start_timestamp and stop_timestamp:
        start_time = datetime.fromtimestamp(int(start_timestamp), tz=timezone.utc)
        end_time = datetime.fromtimestamp(int(stop_timestamp), tz=timezone.utc)
    elif program.get("start_time") and program.get("end_time"):
        start_time = datetime.fromisoformat(program["start_time"])
        end_time = datetime.fromisoformat(program["end_time"])
    else:
        raise ValueError("Program start/end time missing")

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    if start_timestamp and stop_timestamp:
        start_ts = int(start_timestamp)
        stop_ts = int(stop_timestamp)
    else:
        start_ts = int(start_time.timestamp())
        stop_ts = int(end_time.timestamp())

    duration_minutes = int((stop_ts - start_ts) / 60)
    if duration_minutes <= 0:
        raise ValueError("Invalid program duration")

    provider_start = program.get("provider_start")
    provider_stop = program.get("provider_stop")
    if provider_start is not None:
        provider_start = str(provider_start).strip() or None
    if provider_stop is not None:
        provider_stop = str(provider_stop).strip() or None

    return start_time, end_time, start_ts, stop_ts, duration_minutes, provider_start, provider_stop


def _sanitize_filename(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    filename = name
    if not filename.endswith(".ts"):
        filename += ".ts"
    return file_namer.sanitize_filename(filename.removesuffix(".ts")) + ".ts"


def _map_download_status(status: str) -> str:
    if status == "pending":
        return ScheduledStatus.QUEUED.value
    if status == "downloading":
        return ScheduledStatus.DOWNLOADING.value
    if status == "processing":
        return ScheduledStatus.PROCESSING.value
    if status == "completed":
        return ScheduledStatus.COMPLETED.value
    if status == "failed":
        return ScheduledStatus.FAILED.value
    if status == "cancelled":
        return ScheduledStatus.CANCELLED.value
    return ScheduledStatus.QUEUED.value


@router.get("")
async def list_schedules(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(ScheduledRecording).order_by(ScheduledRecording.program_start.desc())
    if not auth.is_admin:
        query = query.where(ScheduledRecording.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    schedules = result.scalars().all()

    download_ids = [s.download_id for s in schedules if s.download_id]
    downloads = {}
    if download_ids:
        download_result = await session.execute(
            select(Download).where(Download.id.in_(download_ids))
        )
        downloads = {d.id: d for d in download_result.scalars().all()}

    response = []
    for schedule in schedules:
        download = downloads.get(schedule.download_id)
        data = schedule.to_dict()
        if download:
            data["status"] = _map_download_status(download.status)
            data["download_status"] = download.status
            data["download_progress"] = download.progress
            data["download_output_path"] = download.output_path
        response.append(data)

    return response


@router.post("")
async def create_schedule(
    data: ScheduleCreate,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == data.account_id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        start_time, end_time, start_ts, stop_ts, duration_minutes, provider_start, provider_stop = _parse_program(data.program)
    except ValueError:
        logger.exception(
            "Invalid schedule payload account_id=%s channel_id=%s",
            data.account_id,
            data.channel_id,
        )
        raise HTTPException(status_code=400, detail="Invalid program payload")

    now_utc = datetime.now(timezone.utc)
    if stop_ts <= int(now_utc.timestamp()):
        raise HTTPException(status_code=400, detail="Program already ended; download instead")

    epg_id = data.program.get("epg_id")
    program_id = data.program.get("id")

    active_statuses = [
        ScheduledStatus.SCHEDULED.value,
        ScheduledStatus.PAUSED_LOW_SPACE.value,
        ScheduledStatus.QUEUED.value,
        ScheduledStatus.DOWNLOADING.value,
        ScheduledStatus.PROCESSING.value,
    ]

    if epg_id:
        conditions = [
            ScheduledRecording.account_id == data.account_id,
            ScheduledRecording.channel_id == data.channel_id,
            ScheduledRecording.epg_id == epg_id,
            ScheduledRecording.start_timestamp == start_ts,
            ScheduledRecording.stop_timestamp == stop_ts,
            ScheduledRecording.status.in_(active_statuses),
        ]
        if not auth.is_admin:
            conditions.append(ScheduledRecording.requested_by_user_id == auth.user_id)
        existing = await session.execute(select(ScheduledRecording).where(*conditions))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="This program is already scheduled")
    elif program_id is not None:
        conditions = [
            ScheduledRecording.account_id == data.account_id,
            ScheduledRecording.channel_id == data.channel_id,
            ScheduledRecording.program_id == str(program_id),
            ScheduledRecording.start_timestamp == start_ts,
            ScheduledRecording.stop_timestamp == stop_ts,
            ScheduledRecording.status.in_(active_statuses),
        ]
        if not auth.is_admin:
            conditions.append(ScheduledRecording.requested_by_user_id == auth.user_id)
        existing = await session.execute(select(ScheduledRecording).where(*conditions))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="This program is already scheduled")

    custom_filename = _sanitize_filename(data.custom_filename)

    schedule = ScheduledRecording(
        account_id=data.account_id,
        channel_id=data.channel_id,
        channel_name=data.channel_name,
        program_id=str(program_id) if program_id is not None else None,
        epg_id=str(epg_id) if epg_id is not None else None,
        program_title=data.program.get("title", "Unknown"),
        program_description=data.program.get("description") or None,
        program_start=start_time,
        program_end=end_time,
        start_timestamp=start_ts,
        stop_timestamp=stop_ts,
        provider_start=provider_start,
        provider_stop=provider_stop,
        duration_minutes=duration_minutes,
        pre_padding_minutes=int(data.pre_padding_minutes or 0),
        post_padding_minutes=int(data.post_padding_minutes or 0),
        custom_filename=custom_filename,
        status=ScheduledStatus.SCHEDULED.value,
        requested_by_user_id=auth.user_id,
        request_source=auth.provider or "admin_local",
    )

    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)

    return schedule.to_dict()


@router.delete("/{schedule_id}")
async def cancel_schedule(
    schedule_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    query = select(ScheduledRecording).where(ScheduledRecording.id == schedule_id)
    if not auth.is_admin:
        query = query.where(ScheduledRecording.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    schedule = result.scalar_one_or_none()

    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if schedule.status in [
        ScheduledStatus.SCHEDULED.value,
        ScheduledStatus.PAUSED_LOW_SPACE.value,
        ScheduledStatus.QUEUED.value,
        ScheduledStatus.DOWNLOADING.value,
        ScheduledStatus.PROCESSING.value,
    ]:
        if schedule.download_id:
            await download_manager.cancel_download(schedule.download_id)
        schedule.status = ScheduledStatus.CANCELLED.value
        schedule.status_message = "Cancelled by user"
        schedule.updated_at = datetime.utcnow()
        await session.commit()
        return {"status": "cancelled"}

    await session.delete(schedule)
    await session.commit()
    return {"status": "deleted"}
