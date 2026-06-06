from datetime import datetime, timedelta, timezone
from typing import Optional
import os
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Download, DownloadStatus, AppSettings, XtreamAccount
from services.account_credentials import resolve_account_password_with_migration
from services.file_namer import file_namer
from services.epg_service import epg_service
from services.xtream_client import XtreamClient
from config import settings as app_settings


def _parse_program_times(program: dict) -> tuple[datetime, datetime, int, datetime, datetime]:
    start_timestamp = program.get("start_timestamp")
    stop_timestamp = program.get("stop_timestamp")

    if start_timestamp and stop_timestamp:
        program_start_utc = datetime.fromtimestamp(int(start_timestamp), tz=timezone.utc)
        program_end_utc = datetime.fromtimestamp(int(stop_timestamp), tz=timezone.utc)
        duration_minutes = int((program_end_utc - program_start_utc).total_seconds() / 60)
    else:
        start_time = program.get("start_time")
        end_time = program.get("end_time")
        if not start_time or not end_time:
            raise ValueError("Program has no valid start or end time")
        program_start_utc = datetime.fromisoformat(start_time)
        program_end_utc = datetime.fromisoformat(end_time)
        duration_minutes = int((program_end_utc - program_start_utc).total_seconds() / 60)

    if program.get("start_time") and program.get("end_time"):
        program_start_local = datetime.fromisoformat(program["start_time"])
        program_end_local = datetime.fromisoformat(program["end_time"])
    else:
        program_start_local = program_start_utc
        program_end_local = program_end_utc

    return program_start_utc, program_end_utc, duration_minutes, program_start_local, program_end_local


def _resolve_provider_start(
    program: dict,
    fallback_start_utc: datetime,
    pre_padding_minutes: int,
) -> tuple[datetime, Optional[str]]:
    provider_start = program.get("provider_start")
    if provider_start is not None:
        provider_start = str(provider_start).strip()
    normalized_provider_start = None
    if provider_start:
        normalized_provider_start = _normalize_provider_start_token(provider_start, pre_padding_minutes)
    if normalized_provider_start:
        return fallback_start_utc, normalized_provider_start

    return fallback_start_utc, None


def _normalize_provider_start_token(provider_start: str, pre_padding_minutes: int) -> Optional[str]:
    token = (provider_start or "").strip()
    if not token:
        return None

    parsed = None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}:\d{2}-\d{2}", token):
        parsed = datetime.strptime(token, "%Y-%m-%d:%H-%M")
    else:
        # Strip trailing timezone offset (e.g. " +0200", " -0500", " +0000") before
        # trying compact formats.  The datetime part without the offset is the
        # provider's local wall-clock time, which is what the timeshift URL expects.
        bare = re.sub(r"\s*([+-]\d{2}:?\d{2}|Z)$", "", token).strip()
        for fmt, candidate in (
            ("%Y-%m-%d %H:%M:%S", bare),
            ("%Y-%m-%d %H:%M",    bare),
            ("%Y-%m-%dT%H:%M:%S", bare),
            ("%Y-%m-%dT%H:%M",    bare),
            ("%Y%m%d%H%M%S",      bare),
            ("%Y%m%d%H%M",        bare),
        ):
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    if pre_padding_minutes:
        parsed = parsed - timedelta(minutes=int(pre_padding_minutes or 0))
    return parsed.strftime("%Y-%m-%d:%H-%M")


async def build_download_from_program(
    session: AsyncSession,
    account_id: int,
    channel_id: str,
    channel_name: str,
    program: dict,
    custom_filename: Optional[str] = None,
    pre_padding_minutes: int = 0,
    post_padding_minutes: int = 0,
    requested_by_user_id: Optional[int] = None,
    request_source: str = "admin",
) -> Download:
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise ValueError("Account not found")

    settings_result = await session.execute(select(AppSettings))
    settings = settings_result.scalar_one_or_none()
    download_folder = settings.download_folder if settings and settings.download_folder else app_settings.default_download_folder

    program_start_utc, program_end_utc, duration_minutes, program_start, program_end = _parse_program_times(program)

    if duration_minutes <= 0:
        raise ValueError("Invalid program duration")

    channel = {
        "name": channel_name,
        "stream_id": channel_id,
        "category_name": program.get("category", ""),
    }

    if custom_filename:
        filename = custom_filename
        if not filename.endswith(".ts"):
            filename += ".ts"
        filename = file_namer.sanitize_filename(filename.removesuffix(".ts")) + ".ts"
    else:
        program_type = epg_service.detect_program_type(program, channel)
        filename = file_namer.generate_filename(program, channel, program_type, settings.to_dict() if settings else None)

    pre_padding = int(pre_padding_minutes or 0)
    post_padding = int(post_padding_minutes or 0)
    padded_start_utc = program_start_utc
    padded_duration = duration_minutes
    if pre_padding:
        padded_start_utc = program_start_utc - timedelta(minutes=pre_padding)
        padded_duration += pre_padding
    if post_padding:
        padded_duration += post_padding

    fallback_url_start, provider_start = _resolve_provider_start(
        program,
        program_start_utc,
        pre_padding,
    )
    padded_url_start = fallback_url_start
    if pre_padding:
        padded_url_start = fallback_url_start - timedelta(minutes=pre_padding)

    password = await resolve_account_password_with_migration(session, account)
    client = XtreamClient(account.server_url, account.username, password)
    source_url = client.build_timeshift_url(
        channel_id,
        padded_url_start,
        padded_duration,
        provider_start=provider_start,
    )

    return Download(
        account_id=account_id,
        channel_id=channel_id,
        channel_name=channel_name,
        program_title=program.get("title", "Unknown"),
        program_start=program_start,
        program_end=program_end,
        start_timestamp=int(program_start_utc.timestamp()),
        stop_timestamp=int(program_end_utc.timestamp()),
        duration_minutes=padded_duration,
        source_url=source_url,
        output_path=os.path.join(download_folder, filename),
        status=DownloadStatus.PENDING.value,
        requested_by_user_id=requested_by_user_id,
        request_source=request_source,
    )
