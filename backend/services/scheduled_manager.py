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


class ScheduledManager:
    def __init__(self):
        self._running = False
        self._poll_interval = 30
        self._last_loop_error_at: datetime | None = None

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
                    if program_end_utc <= catchup_expiry:
                        age = now_utc - program_end_utc
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

            if not ready:
                await session.commit()
                return

            free_gb = self._get_free_space_gb(download_folder)

            for schedule in ready:
                if free_gb < min_free_gb:
                    schedule.status = ScheduledStatus.PAUSED_LOW_SPACE.value
                    schedule.status_message = (
                        f"Waiting for free space ({free_gb:.1f} GB free, "
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

                    download = await download_manager.queue_download(download)

                    # Re-read status: user may have cancelled while we were
                    # awaiting build_download_from_program or queue_download.
                    await session.refresh(schedule)
                    if schedule.status not in (
                        ScheduledStatus.SCHEDULED.value,
                        ScheduledStatus.PAUSED_LOW_SPACE.value,
                    ):
                        await download_manager.cancel_download(download.id)
                        continue

                    schedule.download_id = download.id
                    schedule.status = ScheduledStatus.QUEUED.value
                    schedule.status_message = None
                    schedule.updated_at = datetime.utcnow()
                    await session.commit()
                except Exception as exc:
                    schedule.status = ScheduledStatus.FAILED.value
                    schedule.status_message = str(exc)
                    schedule.updated_at = datetime.utcnow()
                    await session.commit()

            await session.commit()

    async def _get_catchup_window_days(self, session, schedule) -> int:
        # NoCatchupSupportError propagates; other exceptions propagate so caller
        # can hold the schedule in SCHEDULED state rather than dispatching blindly.
        days = await epg_service.get_channel_archive_days(
            session, schedule.account_id, schedule.channel_id
        )
        return days if days > 0 else 7

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
