"""Time slot recording: record a hand-picked time range on a catchup channel.

A time slot has no EPG program behind it, so the caller supplies the start and
stop directly. Everything downstream is the existing catchup pipeline: a slot
that has already finished airing becomes a download straight away, and one that
is still to air becomes a scheduled recording that waits for the provider's
archive. See docs/adr/0004-time-slot-recording-uses-catchup-not-live.md.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin_or_download_user, AuthContext
from database import get_session
from models import Download, DownloadStatus, ScheduledRecording, ScheduledStatus, XtreamAccount
from api.schedules import ScheduleCreate, _create_schedule_record
from services.disk_space import check_disk_space
from services.download_builder import build_download_from_program
from services.download_manager import download_manager
from services.epg_service import epg_service, NoCatchupSupportError
from schedule_timing import get_scheduled_download_delay_minutes


router = APIRouter()
logger = logging.getLogger(__name__)

# A time slot is typed by hand, so it needs bounds that a guide-picked
# programme gets for free from the provider.
MAX_TIME_SLOT_MINUTES = 6 * 60
MAX_FUTURE_DAYS = 14
# Matches the fallback scheduled_manager uses when a provider does not report a
# per-channel archive depth.
DEFAULT_ARCHIVE_DAYS = 7

ACTIVE_DOWNLOAD_STATUSES = [
    DownloadStatus.PENDING.value,
    DownloadStatus.DOWNLOADING.value,
    DownloadStatus.PROCESSING.value,
]

ACTIVE_SCHEDULE_STATUSES = [
    ScheduledStatus.SCHEDULED.value,
    ScheduledStatus.PAUSED_LOW_SPACE.value,
    ScheduledStatus.QUEUED.value,
    ScheduledStatus.DOWNLOADING.value,
    ScheduledStatus.PROCESSING.value,
]


class TimeSlotCreate(BaseModel):
    account_id: int
    channel_id: str
    channel_name: str
    title: str = Field(min_length=1, max_length=500)
    start_timestamp: int
    stop_timestamp: int
    # Overlapping slots are legitimate (a wide safety recording plus a tight
    # one), so an overlap warns once rather than blocking.
    confirm_overlap: bool = False


def validate_time_slot(
    start_ts: int,
    stop_ts: int,
    archive_days: int,
    now_ts: int,
) -> None:
    """Raise ValueError with a user-facing message if the range is not recordable."""
    duration_minutes = int((stop_ts - start_ts) / 60)
    if stop_ts <= start_ts:
        raise ValueError("The end time must be after the start time.")
    if duration_minutes < 1:
        raise ValueError("A time slot must be at least one minute long.")
    if duration_minutes > MAX_TIME_SLOT_MINUTES:
        raise ValueError(
            f"A time slot can be at most {MAX_TIME_SLOT_MINUTES // 60} hours long."
        )

    days = int(archive_days) if archive_days and archive_days > 0 else DEFAULT_ARCHIVE_DAYS
    earliest = now_ts - days * 86400
    if start_ts < earliest:
        raise ValueError(
            f"That start time is outside this channel's catchup window. "
            f"This channel keeps {days} days of catchup."
        )

    latest = now_ts + MAX_FUTURE_DAYS * 86400
    if start_ts > latest:
        raise ValueError(
            f"A time slot can start at most {MAX_FUTURE_DAYS} days in the future."
        )


def build_time_slot_program(
    title: str,
    start_ts: int,
    stop_ts: int,
    guide_offset_hours: int = 0,
) -> dict:
    """Build the program payload the download/schedule pipeline expects.

    A guide-picked programme carries the provider's own start token, which is
    the provider's local wall-clock time. A time slot has none, so derive it:
    the guide offset is what makes displayed times line up with the channel's
    clock, so provider wall-clock is UTC plus that offset.
    """
    start_utc = datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
    provider_start_dt = start_utc + timedelta(hours=int(guide_offset_hours or 0))
    return {
        "title": title,
        "start_timestamp": int(start_ts),
        "stop_timestamp": int(stop_ts),
        "provider_start": provider_start_dt.strftime("%Y-%m-%d:%H-%M"),
    }


async def _find_overlap(
    session: AsyncSession,
    account_id: int,
    channel_id: str,
    start_ts: int,
    stop_ts: int,
) -> Optional[str]:
    """Return a human description of an active recording overlapping this range."""
    download_q = select(Download).where(
        Download.account_id == account_id,
        Download.channel_id == channel_id,
        Download.status.in_(ACTIVE_DOWNLOAD_STATUSES),
        Download.start_timestamp < stop_ts,
        Download.stop_timestamp > start_ts,
    ).limit(1)
    existing = (await session.execute(download_q)).scalar_one_or_none()
    if existing is not None:
        return existing.program_title

    schedule_q = select(ScheduledRecording).where(
        ScheduledRecording.account_id == account_id,
        ScheduledRecording.channel_id == channel_id,
        ScheduledRecording.status.in_(ACTIVE_SCHEDULE_STATUSES),
        ScheduledRecording.start_timestamp < stop_ts,
        ScheduledRecording.stop_timestamp > start_ts,
    ).limit(1)
    scheduled = (await session.execute(schedule_q)).scalar_one_or_none()
    if scheduled is not None:
        return scheduled.program_title

    return None


async def _resolve_archive_days(
    session: AsyncSession,
    account_id: int,
    channel_id: str,
) -> int:
    """Per-channel catchup depth, or the default when the provider is unreachable.

    A provider outage should not block a recording the user can still describe;
    the scheduled path already fails gracefully later if the window is wrong.
    """
    try:
        return await epg_service.get_channel_archive_days(session, account_id, channel_id)
    except NoCatchupSupportError:
        raise
    except Exception:
        logger.warning(
            "Could not resolve catchup window for account_id=%s channel_id=%s; "
            "falling back to %s days",
            account_id,
            channel_id,
            DEFAULT_ARCHIVE_DAYS,
        )
        return DEFAULT_ARCHIVE_DAYS


@router.post("")
async def create_time_slot(
    data: TimeSlotCreate,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Record a hand-picked time range. Past ranges download now, future ones wait."""
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Give the recording a name.")

    account = (
        await session.execute(
            select(XtreamAccount).where(XtreamAccount.id == data.account_id)
        )
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        archive_days = await _resolve_archive_days(session, data.account_id, data.channel_id)
    except NoCatchupSupportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    now_ts = int(datetime.now(timezone.utc).timestamp())
    try:
        validate_time_slot(data.start_timestamp, data.stop_timestamp, archive_days, now_ts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not data.confirm_overlap:
        overlap = await _find_overlap(
            session,
            data.account_id,
            data.channel_id,
            data.start_timestamp,
            data.stop_timestamp,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "overlap",
                    "message": f"This overlaps \"{overlap}\", which is already recording on this channel.",
                },
            )

    program = build_time_slot_program(
        title,
        data.start_timestamp,
        data.stop_timestamp,
        guide_offset_hours=int(account.guide_offset_hours or 0),
    )

    # A slot that has finished airing is already in the archive; one that has
    # not waits for it. Time slots never carry padding, so the recorded range
    # is exactly the range entered.
    if data.stop_timestamp <= now_ts:
        return await _queue_time_slot_download(session, data, program, auth)

    schedule = await _create_schedule_record(
        ScheduleCreate(
            account_id=data.account_id,
            channel_id=data.channel_id,
            channel_name=data.channel_name,
            program=program,
            pre_padding_minutes=0,
            post_padding_minutes=0,
        ),
        auth,
        session,
    )
    download_delay_minutes = await get_scheduled_download_delay_minutes(session)
    return {
        "kind": "schedule",
        "record": schedule.to_dict(download_delay_minutes),
    }


async def _queue_time_slot_download(
    session: AsyncSession,
    data: TimeSlotCreate,
    program: dict,
    auth: AuthContext,
):
    try:
        download = await build_download_from_program(
            session,
            account_id=data.account_id,
            channel_id=data.channel_id,
            channel_name=data.channel_name,
            program=program,
            pre_padding_minutes=0,
            post_padding_minutes=0,
            requested_by_user_id=auth.user_id,
            request_source=auth.provider or "admin_local",
        )
    except ValueError as exc:
        message = str(exc)
        if "Account not found" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)

    duplicate = await session.execute(
        select(Download.id).where(
            Download.account_id == download.account_id,
            Download.channel_id == download.channel_id,
            Download.start_timestamp == download.start_timestamp,
            Download.stop_timestamp == download.stop_timestamp,
            Download.status.in_(ACTIVE_DOWNLOAD_STATUSES),
        ).limit(1)
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="A download for this time slot is already active.")

    await check_disk_space(session)

    try:
        download = await download_manager.queue_download(download)
    except ValueError:
        raise HTTPException(status_code=409, detail="A download for this output path is already active.")

    return {"kind": "download", "record": download.to_dict()}
