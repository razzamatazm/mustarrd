import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from database import async_session_maker
from models import ScheduledRecording, ScheduledStatus, AppSettings
from services.download_builder import build_download_from_program
from services.download_manager import download_manager
from services.epg_service import epg_service, NoCatchupSupportError
from config import settings as app_settings
import os
import shutil

logger = logging.getLogger(__name__)

# Rough size estimate used to project disk usage of recordings dispatched in
# the same tick, before any of them has written bytes to disk. A typical HD
# IPTV TS stream runs about 4-7 Mbps (~2-3 GB/hour); over-estimating slightly
# errs on the side of pausing instead of filling the disk.
ESTIMATED_RECORDING_GB_PER_HOUR = 3.0


class ScheduledManager:
    def __init__(self):
        self._running = False
        self._poll_interval = 30
        self._last_loop_error_at: datetime | None = None
        # Per-tick cache of provider channel lists, keyed by account_id.
        # Holds either the fetched list or the exception the fetch raised, so
        # each account's provider is called at most once per tick.
        self._tick_channel_lists: dict = {}

    async def process_queue(self):
        self._running = True

        while self._running:
            try:
                await self._queue_ready_recordings()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._should_log_loop_error():
                    logger.exception("Error in schedule processor")
            await asyncio.sleep(self._poll_interval)

    async def _queue_ready_recordings(self):
        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        self._tick_channel_lists = {}
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledRecording).where(
                    ScheduledRecording.status.in_(
                        [
                            ScheduledStatus.SCHEDULED.value,
                            ScheduledStatus.PAUSED_LOW_SPACE.value,
                        ]
                    )
                )
            )
            schedules = result.scalars().all()

            if not schedules:
                return

            settings_result = await session.execute(select(AppSettings))
            settings = settings_result.scalar_one_or_none()
            download_folder = settings.download_folder if settings and settings.download_folder else app_settings.default_download_folder
            min_free_gb = settings.min_free_space_gb if settings and settings.min_free_space_gb is not None else 25

            ready = []
            for schedule in schedules:
                available_at = schedule.available_at_utc()
                if not available_at:
                    continue
                if available_at <= now_utc:
                    try:
                        archive_days = await self._get_catchup_window_days(session, schedule)
                    except NoCatchupSupportError as exc:
                        schedule.status = ScheduledStatus.FAILED.value
                        schedule.status_message = str(exc)
                        schedule.updated_at = datetime.utcnow()
                        continue
                    except Exception:
                        logger.warning(
                            "Could not check catchup window for schedule %s; will retry next poll",
                            schedule.id,
                        )
                        continue
                    catchup_expiry = now_utc - timedelta(days=archive_days)
                    program_end_utc = available_at - timedelta(minutes=int(schedule.post_padding_minutes or 0))
                    # Timeshift URLs are keyed to program start; compare padded start
                    # against the boundary, not program end.
                    if schedule.start_timestamp:
                        prog_start_utc = datetime.fromtimestamp(int(schedule.start_timestamp), tz=timezone.utc)
                    else:
                        prog_start_utc = program_end_utc - timedelta(minutes=int(schedule.duration_minutes or 0))
                    padded_start_utc = prog_start_utc - timedelta(minutes=int(schedule.pre_padding_minutes or 0))
                    if padded_start_utc <= catchup_expiry:
                        age = now_utc - prog_start_utc
                        total_hours = int(age.total_seconds() / 3600)
                        age_str = f"about {age.days} days" if age.days >= 2 else f"about {total_hours} hours"
                        schedule.status = ScheduledStatus.FAILED.value
                        schedule.status_message = (
                            f"Program is no longer available for catchup. "
                            f"It aired {age_str} ago, past the {archive_days}-day catchup window."
                        )
                        schedule.updated_at = datetime.utcnow()
                        continue
                    ready.append(schedule)

            # Persist FAILED marks for expired/no-catchup schedules now, so a
            # per-schedule rollback in the dispatch loop below cannot discard them.
            await session.commit()

            if not ready:
                return

            free_gb = self._get_free_space_gb(download_folder)
            # Free space is read once per tick, but recordings dispatched in
            # this tick have not written any bytes yet. Track their expected
            # size so a batch of N due schedules cannot collectively dispatch
            # past the minimum free space threshold.
            projected_used_gb = 0.0

            for schedule in ready:
                available_gb = free_gb - projected_used_gb
                if available_gb < min_free_gb:
                    schedule.status = ScheduledStatus.PAUSED_LOW_SPACE.value
                    schedule.status_message = (
                        f"Waiting for free space ({available_gb:.1f} GB free, "
                        f"{min_free_gb} GB required)."
                    )
                    schedule.updated_at = datetime.utcnow()
                    continue

                try:
                    program = {
                        "title": schedule.program_title,
                        "description": schedule.program_description or "",
                        "start_time": schedule.program_start.isoformat() if schedule.program_start else None,
                        "end_time": schedule.program_end.isoformat() if schedule.program_end else None,
                        "start_timestamp": schedule.start_timestamp,
                        "stop_timestamp": schedule.stop_timestamp,
                        "provider_start": schedule.provider_start,
                        "provider_stop": schedule.provider_stop,
                        "duration_minutes": schedule.duration_minutes,
                        "epg_id": schedule.epg_id,
                        "id": schedule.program_id,
                        "category": schedule.channel_category_name or "",
                    }

                    download = await build_download_from_program(
                        session,
                        account_id=schedule.account_id,
                        channel_id=schedule.channel_id,
                        channel_name=schedule.channel_name,
                        program=program,
                        custom_filename=schedule.custom_filename,
                        pre_padding_minutes=schedule.pre_padding_minutes,
                        post_padding_minutes=schedule.post_padding_minutes,
                        requested_by_user_id=schedule.requested_by_user_id,
                        request_source=schedule.request_source or "admin",
                    )

                    # Stage the download row on this session so it commits
                    # atomically with the schedule update below. A commit
                    # failure leaves neither row behind, so the next tick can
                    # retry without producing a duplicate download.
                    download = await download_manager.queue_download(download, session=session)

                    # Re-read status: user may have cancelled while we were
                    # awaiting build_download_from_program or queue_download.
                    await session.refresh(schedule)
                    if schedule.status not in (
                        ScheduledStatus.SCHEDULED.value,
                        ScheduledStatus.PAUSED_LOW_SPACE.value,
                    ):
                        # Discard the staged (uncommitted) download row.
                        await session.rollback()
                        continue

                    schedule.download_id = download.id
                    schedule.status = ScheduledStatus.QUEUED.value
                    schedule.status_message = None
                    schedule.updated_at = datetime.utcnow()
                    await session.commit()
                    await download_manager.enqueue_persisted(download)
                    projected_used_gb += self._estimate_recording_gb(schedule)
                except Exception as exc:
                    await session.rollback()
                    schedule.status = ScheduledStatus.FAILED.value
                    schedule.status_message = str(exc)
                    schedule.updated_at = datetime.utcnow()
                    await session.commit()

            await session.commit()

    async def _get_catchup_window_days(self, session, schedule) -> int:
        # NoCatchupSupportError propagates; other exceptions propagate so caller
        # can hold the schedule in SCHEDULED state rather than dispatching blindly.
        # The channel list is fetched at most once per account per tick (results
        # and failures are both cached) so a batch of due schedules does not
        # hammer the provider with one get_live_streams() call each.
        account_id = schedule.account_id
        channels = self._tick_channel_lists.get(account_id)
        if channels is None:
            try:
                channels = await epg_service.get_account_live_streams(session, account_id)
            except Exception as exc:
                channels = exc
            self._tick_channel_lists[account_id] = channels
        if isinstance(channels, Exception):
            raise channels
        days = epg_service.archive_days_from_channels(channels, schedule.channel_id)
        return days if days > 0 else 7

    def _estimate_recording_gb(self, schedule) -> float:
        """Expected on-disk size of a recording, including padding."""
        minutes = int(schedule.duration_minutes or 0)
        minutes += int(schedule.pre_padding_minutes or 0)
        minutes += int(schedule.post_padding_minutes or 0)
        return max(minutes, 0) / 60.0 * ESTIMATED_RECORDING_GB_PER_HOUR

    def _get_free_space_gb(self, path: str) -> float:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)

    def _should_log_loop_error(self, cooldown_seconds: int = 60) -> bool:
        now = datetime.utcnow()
        if self._last_loop_error_at is None:
            self._last_loop_error_at = now
            return True
        if (now - self._last_loop_error_at).total_seconds() >= cooldown_seconds:
            self._last_loop_error_at = now
            return True
        return False


scheduled_manager = ScheduledManager()
