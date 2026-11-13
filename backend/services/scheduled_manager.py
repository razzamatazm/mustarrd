import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy import exc as sa_exc
from database import async_session_maker
from models import ScheduledRecording, ScheduledStatus, AppSettings, Download, DownloadStatus
from services.download_builder import build_download_from_program
from services.download_manager import download_manager
from services.epg_service import epg_service, NoCatchupSupportError
from services.disk_space import get_free_space_gb
from services.retry_policy import AUTO_RETRY_MAX_ATTEMPTS, AUTO_RETRY_MIN_INTERVAL_MINUTES
from config import settings as app_settings
from schedule_timing import resolve_scheduled_download_delay_minutes

logger = logging.getLogger(__name__)

# Rough size estimate used to project disk usage of recordings dispatched in
# the same tick, before any of them has written bytes to disk. A typical HD
# IPTV TS stream runs about 4-7 Mbps (~2-3 GB/hour); over-estimating slightly
# errs on the side of pausing instead of filling the disk.
ESTIMATED_RECORDING_GB_PER_HOUR = 3.0

# Failed-download recovery, including opted-in short-file failures, uses one
# master switch and the shared bounded retry policy.

# Dispatching a due schedule (build the download row, commit it with the
# schedule update) can fail for two very different reasons. Infrastructure
# failures - SQLite "database is locked", a dropped connection, a pool timeout
# - are transient: the same work succeeds moments later. Everything else
# (unusable program data, a builder ValueError) fails the same way every time.
# Transient failures get a few quick in-tick retries, and if they all fail the
# schedule stays dispatchable so the next poll picks it up again. There is
# deliberately no cross-tick attempt limit: counting attempts across ticks
# would mean a new column on scheduled_recordings, and a schedule whose
# database is down for good stops being retried anyway once its program falls
# out of the catchup window (which fails it with a clear message). Each failed
# attempt is logged, and the schedule carries a "will retry" status_message in
# the meantime.
DISPATCH_MAX_ATTEMPTS = 3
DISPATCH_RETRY_BACKOFF_SECONDS = 0.2  # base delay, multiplied by attempt number

_TRANSIENT_DISPATCH_ERRORS = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.InternalError,
    sa_exc.TimeoutError,
    sa_exc.DisconnectionError,
    ConnectionError,
)


def _is_transient_dispatch_error(exc: BaseException) -> bool:
    """True when a dispatch failure is infrastructure, not this schedule.

    Deterministic errors must stay terminal on the first attempt - retrying
    them only delays a failure the user needs to see.
    """
    return isinstance(exc, _TRANSIENT_DISPATCH_ERRORS)


class ScheduledManager:
    def __init__(self):
        self._running = False
        self._poll_interval = 30
        self._last_loop_error_at: datetime | None = None
        # Per-tick cache of provider channel lists, keyed by account_id.
        # Holds either the fetched list or the exception the fetch raised, so
        # each account's provider is called at most once per tick.
        self._tick_channel_lists: dict = {}
        # Download ids the auto-retry sweep determined can never become
        # eligible again (program aged out of the archive window, channel has
        # no catchup support, no usable program end). Kept in memory so the
        # sweep does not re-query the provider for them every tick; a restart
        # re-evaluates once.
        self._auto_retry_ineligible: set = set()

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
            try:
                await self._auto_retry_failed_downloads()
            except asyncio.CancelledError:
                break
            except Exception:
                if self._should_log_loop_error():
                    logger.exception("Error in auto-retry sweep")
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
            download_delay_minutes = resolve_scheduled_download_delay_minutes(
                settings.scheduled_download_delay_minutes if settings else None
            )

            ready = []
            for schedule in schedules:
                archive_ready_at = schedule.available_at_utc()
                if not archive_ready_at:
                    continue
                download_at = schedule.available_at_utc(download_delay_minutes)
                if download_at <= now_utc:
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
                    program_end_utc = archive_ready_at - timedelta(minutes=int(schedule.post_padding_minutes or 0))
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

            free_gb = await self._get_free_space_gb(download_folder)
            # Free space is read once per tick, but recordings dispatched in
            # this tick have not written any bytes yet. Track their expected
            # size so a batch of N due schedules cannot collectively dispatch
            # past the minimum free space threshold.
            projected_used_gb = 0.0

            # Snapshot all ORM attributes that the dispatch loop will read,
            # before entering the loop. session.rollback() in a prior
            # iteration's except handler expires every object in the session
            # identity map. Without local copies, the next iteration raises
            # MissingGreenlet when it reads the expired attributes in an async
            # context, and the generic except marks that schedule FAILED with
            # a cryptic SQLAlchemy error.
            ready_data = [
                {
                    "schedule": s,
                    "schedule_id": s.id,
                    "account_id": s.account_id,
                    "channel_id": s.channel_id,
                    "channel_name": s.channel_name,
                    "program_title": s.program_title,
                    "program_description": s.program_description,
                    "program_start": s.program_start,
                    "program_end": s.program_end,
                    "start_timestamp": s.start_timestamp,
                    "stop_timestamp": s.stop_timestamp,
                    "provider_start": s.provider_start,
                    "provider_stop": s.provider_stop,
                    "duration_minutes": s.duration_minutes,
                    "epg_id": s.epg_id,
                    "program_id": s.program_id,
                    "channel_category_name": s.channel_category_name,
                    "custom_filename": s.custom_filename,
                    "pre_padding_minutes": s.pre_padding_minutes,
                    "post_padding_minutes": s.post_padding_minutes,
                    "requested_by_user_id": s.requested_by_user_id,
                    "request_source": s.request_source,
                }
                for s in ready
            ]

            for snap in ready_data:
                schedule = snap["schedule"]

                if free_gb is None:
                    # Folder missing or unreachable (e.g. NAS not mounted).
                    # Fail safe: hold the schedule instead of recording onto
                    # whatever disk happens to back the path right now.
                    schedule.status = ScheduledStatus.PAUSED_LOW_SPACE.value
                    schedule.status_message = (
                        f"Download folder {download_folder} is missing or "
                        f"unreachable. Waiting for it to become available."
                    )
                    schedule.updated_at = datetime.utcnow()
                    continue
                available_gb = free_gb - projected_used_gb
                if available_gb < min_free_gb:
                    schedule.status = ScheduledStatus.PAUSED_LOW_SPACE.value
                    schedule.status_message = (
                        f"Waiting for free space ({available_gb:.1f} GB free, "
                        f"{min_free_gb} GB required)."
                    )
                    schedule.updated_at = datetime.utcnow()
                    continue

                # Attempt dispatch a bounded number of times within this tick.
                # A transient infrastructure error is worth an immediate retry;
                # anything else is this schedule's own fault and fails now.
                dispatched = False
                pending_transient: Exception | None = None
                for attempt in range(1, DISPATCH_MAX_ATTEMPTS + 1):
                    try:
                        dispatched = await self._dispatch_schedule(session, snap, schedule)
                        pending_transient = None
                        break
                    except Exception as exc:
                        # Discard the staged download row and whatever else the
                        # failed attempt left on the session, so a retry (or
                        # the next tick) starts clean.
                        await session.rollback()

                        if not _is_transient_dispatch_error(exc):
                            # A deterministic failure ends it, even if an
                            # earlier attempt in this tick failed transiently.
                            pending_transient = None
                            schedule.status = ScheduledStatus.FAILED.value
                            schedule.status_message = (
                                f"Could not start this recording: {exc}"
                            )
                            schedule.updated_at = datetime.utcnow()
                            await session.commit()
                            break

                        pending_transient = exc
                        logger.warning(
                            "Transient error dispatching schedule %s (attempt %s/%s): %s",
                            snap["schedule_id"], attempt, DISPATCH_MAX_ATTEMPTS, exc,
                        )
                        if attempt < DISPATCH_MAX_ATTEMPTS:
                            await asyncio.sleep(DISPATCH_RETRY_BACKOFF_SECONDS * attempt)

                if pending_transient is not None:
                    # Every attempt hit infrastructure trouble. Leave the
                    # schedule dispatchable so the next poll tries again
                    # instead of burying a recoverable recording in FAILED.
                    # Re-read first: the user may have cancelled meanwhile,
                    # and a cancelled schedule must not be resurrected.
                    await session.refresh(schedule)
                    if schedule.status not in (
                        ScheduledStatus.SCHEDULED.value,
                        ScheduledStatus.PAUSED_LOW_SPACE.value,
                    ):
                        continue
                    schedule.status = ScheduledStatus.SCHEDULED.value
                    schedule.status_message = (
                        "Temporary problem starting this recording "
                        f"({type(pending_transient).__name__}). Will retry shortly."
                    )
                    schedule.updated_at = datetime.utcnow()
                    await session.commit()

                if dispatched:
                    projected_used_gb += self._estimate_recording_gb(schedule)

            await session.commit()

    async def _dispatch_schedule(self, session, snap, schedule) -> bool:
        """Build, stage and commit one due schedule's download, then enqueue it.

        Returns True when the recording was dispatched, False when the schedule
        was cancelled (or otherwise moved on) while we were building it. Raises
        on failure, leaving the caller to classify the error; the session is
        rolled back by the caller, so a failed attempt stages nothing.
        """
        program = {
            "title": snap["program_title"],
            "description": snap["program_description"] or "",
            "start_time": snap["program_start"].isoformat() if snap["program_start"] else None,
            "end_time": snap["program_end"].isoformat() if snap["program_end"] else None,
            "start_timestamp": snap["start_timestamp"],
            "stop_timestamp": snap["stop_timestamp"],
            "provider_start": snap["provider_start"],
            "provider_stop": snap["provider_stop"],
            "duration_minutes": snap["duration_minutes"],
            "epg_id": snap["epg_id"],
            "id": snap["program_id"],
            "channel_id": snap["channel_id"],
            "category": snap["channel_category_name"] or "",
        }

        # The snapshot saved when the schedule was created carries no
        # structured metadata, so the recording would be named from title text
        # alone. The stored guide entry for this exact airing still has the
        # season and episode.
        await epg_service.fill_gaps_from_stored(
            session, snap["account_id"], [program]
        )

        download = await build_download_from_program(
            session,
            account_id=snap["account_id"],
            channel_id=snap["channel_id"],
            channel_name=snap["channel_name"],
            program=program,
            custom_filename=snap["custom_filename"],
            pre_padding_minutes=snap["pre_padding_minutes"],
            post_padding_minutes=snap["post_padding_minutes"],
            requested_by_user_id=snap["requested_by_user_id"],
            request_source=snap["request_source"] or "admin",
        )

        # Stage the download row on this session so it commits atomically with
        # the schedule update below. A commit failure leaves neither row
        # behind, so a retry cannot produce a duplicate download.
        download = await download_manager.queue_download(download, session=session)

        # Re-read status: user may have cancelled while we were awaiting
        # build_download_from_program or queue_download.
        await session.refresh(schedule)
        if schedule.status not in (
            ScheduledStatus.SCHEDULED.value,
            ScheduledStatus.PAUSED_LOW_SPACE.value,
        ):
            # Discard the staged (uncommitted) download row.
            await session.rollback()
            return False

        schedule.download_id = download.id
        schedule.status = ScheduledStatus.QUEUED.value
        schedule.status_message = None
        schedule.updated_at = datetime.utcnow()
        await session.commit()
        # Past the commit the recording exists. Nothing here may raise back
        # into the retry loop: a retry would rebuild and commit a second
        # download row for the same schedule. A queue hand-off that fails is
        # recovered on the next restart instead.
        try:
            await download_manager.enqueue_persisted(download)
        except Exception:
            logger.exception(
                "Schedule %s was dispatched but its download could not be "
                "put on the queue; startup recovery will pick it up",
                snap["schedule_id"],
            )
        return True

    async def _get_archive_days_raw(self, session, account_id: int, channel_id: str) -> int:
        """Channel's archive window in days; 0 when the provider reports none.

        NoCatchupSupportError propagates; other exceptions propagate so callers
        can decide how to handle an unreachable provider. The channel list is
        fetched at most once per account per tick (results and failures are
        both cached) so a batch of due schedules or retry candidates does not
        hammer the provider with one get_live_streams() call each.
        """
        channels = self._tick_channel_lists.get(account_id)
        if channels is None:
            try:
                channels = await epg_service.get_account_live_streams(session, account_id)
            except Exception as exc:
                channels = exc
            self._tick_channel_lists[account_id] = channels
        if isinstance(channels, Exception):
            raise channels
        return epg_service.archive_days_from_channels(channels, channel_id)

    async def _get_catchup_window_days(self, session, schedule) -> int:
        days = await self._get_archive_days_raw(session, schedule.account_id, schedule.channel_id)
        return days if days > 0 else 7

    def _download_program_end_utc(self, download) -> datetime | None:
        if download.stop_timestamp:
            try:
                return datetime.fromtimestamp(int(download.stop_timestamp), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if download.program_end:
            end = download.program_end
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            return end
        return None

    async def _auto_retry_failed_downloads(self):
        """Retry Failed downloads, or finalize held raw captures when recovery ends.

        Every automatic retry requires the failed-download master switch. The
        short-file setting only controls classification, never enables retries.
        """
        now_utc = datetime.now(timezone.utc)
        to_retry, to_finalize = [], []
        async with async_session_maker() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one_or_none()
            if settings is None:
                return
            master = bool(settings.auto_retry_failed_downloads)
            mark_short = bool(getattr(settings, "mark_short_downloads_failed", False))
            query = select(Download).where(
                Download.status == DownloadStatus.FAILED.value,
                Download.is_vod.isnot(True),
                Download.retry_finalized.isnot(True),
            )
            if not master:
                query = query.where(Download.processing_deferred.is_(True))
            rows = (await session.execute(query)).scalars().all()
            if not rows:
                return
            schedules = (await session.execute(select(ScheduledRecording).where(
                ScheduledRecording.download_id.in_([row.id for row in rows])
            ))).scalars().all()
            blocked = {row.download_id for row in schedules if row.status in (
                ScheduledStatus.CANCELLED.value, ScheduledStatus.COMPLETED.value
            )}
            free_gb = None
            disk_checked = False
            for download in rows:
                if download.id in blocked:
                    continue
                held = bool(download.processing_deferred)
                classified_short = bool(download.short_file_failed) or held
                stopped = (not master or (classified_short and not mark_short)
                           or int(download.retry_count or 0) >= AUTO_RETRY_MAX_ATTEMPTS)
                if stopped or download.id in self._auto_retry_ineligible:
                    if held:
                        to_finalize.append((download.id, download.id in self._auto_retry_ineligible))
                    continue
                times = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
                         for t in (download.last_retry_at, download.completed_at) if t is not None]
                if times and now_utc - max(times) < timedelta(minutes=AUTO_RETRY_MIN_INTERVAL_MINUTES):
                    continue
                if not disk_checked:
                    free_gb = await self._get_free_space_gb(settings.download_folder or app_settings.default_download_folder)
                    disk_checked = True
                minimum = settings.min_free_space_gb if settings.min_free_space_gb is not None else 25
                if free_gb is None or free_gb < minimum:
                    continue
                program_end = self._download_program_end_utc(download)
                if program_end is None:
                    self._auto_retry_ineligible.add(download.id)
                    if held:
                        to_finalize.append((download.id, True))
                    continue
                try:
                    archive_days = await self._get_archive_days_raw(session, download.account_id, download.channel_id)
                except NoCatchupSupportError:
                    archive_days = 0
                except Exception:
                    # A temporarily unreachable provider is not confirmed expiry.
                    continue
                if archive_days <= 0 or now_utc >= program_end + timedelta(days=archive_days):
                    self._auto_retry_ineligible.add(download.id)
                    if held:
                        to_finalize.append((download.id, True))
                    continue
                to_retry.append(download.id)
        for download_id, unavailable in to_finalize:
            await download_manager.finalize_deferred_capture(download_id, recovery_unavailable=unavailable)
        for download_id in to_retry:
            # The manager owns the final recheck and reserves the retry count
            # atomically with its Pending transition, not during stale selection.
            requeued = await download_manager.retry_download(download_id, automatic=True)
            logger.info("Auto-retry %s for download %s", "queued" if requeued else "skipped (status changed)", download_id)

    def _estimate_recording_gb(self, schedule) -> float:
        """Expected on-disk size of a recording, including padding."""
        minutes = int(schedule.duration_minutes or 0)
        minutes += int(schedule.pre_padding_minutes or 0)
        minutes += int(schedule.post_padding_minutes or 0)
        return max(minutes, 0) / 60.0 * ESTIMATED_RECORDING_GB_PER_HOUR

    async def _get_free_space_gb(self, path: str) -> float | None:
        """Read-only free-space probe; None when the folder is missing or unreachable.

        Must never create the folder: when a NAS mount is missing, os.makedirs
        would silently create the path on the container's root filesystem and
        the check would pass against the wrong disk, filling the root disk.
        The blocking stat runs off the event loop with a timeout so a hung
        mount cannot stall the scheduler or anything else on the loop.
        """
        return await get_free_space_gb(path)

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
