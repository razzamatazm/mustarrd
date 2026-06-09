import asyncio
import aiohttp
import aiofiles
import errno
import glob as glob_module
import logging
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, Dict, Set, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from models import Download, DownloadStatus, AppSettings, XtreamAccount, PlexServer, ScheduledRecording, ScheduledStatus
from config import settings as app_settings, is_docker_env
from database import async_session_maker
from services.log_stream import backend_log_stream
from services.credential_crypto import credential_crypto
from services.disk_space import get_free_space_shortfall
from services.plex_service import plex_service

logger = logging.getLogger(__name__)

# Bounded retry for transient network errors mid-download. A single provider
# hiccup (connection reset, read timeout) should not permanently fail the
# download; each retry resumes from the bytes already on disk via HTTP Range.
TRANSIENT_RETRY_LIMIT = 3  # retries after the initial attempt
TRANSIENT_RETRY_BACKOFF_SECONDS = 5.0  # base delay, multiplied by attempt number

# Default minimum free space to preserve when the AppSettings row has no value;
# matches the default used by services.disk_space.check_disk_space.
MIN_FREE_SPACE_FALLBACK_GB = 25

# For chunked transfers (no Content-Length) percent progress is unknown, so
# progress is persisted and broadcast every this-many bytes instead.
CHUNKED_PROGRESS_INTERVAL_BYTES = 8 * 1024 * 1024


def _is_transient_network_error(e: BaseException) -> bool:
    """True for network-level errors worth retrying (disconnects, timeouts).

    HTTP status errors are raised by _download_file_once as plain
    ``Exception("HTTP ...")`` and disk errors (e.g. ENOSPC) are plain
    ``OSError``, so neither matches here: they fail fast.
    """
    if isinstance(e, asyncio.TimeoutError):
        return True
    if isinstance(e, aiohttp.ClientResponseError):
        # HTTP-level error (4xx/5xx); not a transient transport failure.
        return False
    if isinstance(e, aiohttp.ClientError):
        return True
    return False


def _friendly_error(e: Exception) -> str:
    """Translate a download exception into a short, user-readable string."""
    import errno
    if isinstance(e, asyncio.TimeoutError):
        return "Connection timed out. The provider took too long to respond."
    if isinstance(e, aiohttp.ClientConnectorError):
        return "Cannot reach the provider. Check the server URL in your account settings."
    if isinstance(e, aiohttp.ServerDisconnectedError):
        return "Provider disconnected unexpectedly. Try again later."
    if isinstance(e, aiohttp.ClientError):
        return "Network error connecting to provider. Try again later."
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "Not enough disk space. Free up space and retry."
    msg = str(e)
    if not msg:
        return "Download failed for an unknown reason. Check the logs for details."
    if msg.startswith("HTTP 401") or msg.startswith("HTTP 403"):
        return "Invalid credentials or not authorized. Check your account settings."
    if msg.startswith("HTTP 404"):
        return "Recording not found on provider. The catchup window may have expired."
    if msg.startswith("HTTP 5"):
        return "Provider server error. Try again later."
    if msg.startswith("HTTP "):
        code = msg.split(":")[0]
        return f"Provider returned an error ({code}). Try again later."
    return msg


class DownloadManager:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._active_downloads: Dict[int, asyncio.Task] = {}
        self._post_queue: asyncio.Queue = asyncio.Queue()
        self._active_post: Dict[int, asyncio.Task] = {}
        self._active_account_ids: Dict[int, int] = {}
        self._account_active_counts: Dict[int, int] = {}
        self._cancelled: Set[int] = set()
        self._progress_callbacks: Dict[int, Callable] = {}
        self._websocket_connections: Dict[Any, Dict[str, Any]] = {}
        self._stage_progress: Dict[int, Dict[str, Any]] = {}
        self._download_owners: Dict[int, int | None] = {}
        self._max_concurrent = 2
        self._max_concurrent_post_processing = 1
        self._running = False
        self._loop_error_logged_at: Dict[str, datetime] = {}

    def set_max_concurrent(self, max_concurrent: int):
        try:
            self._max_concurrent = max(1, int(max_concurrent))
        except Exception:
            self._max_concurrent = 1

    def set_max_concurrent_post_processing(self, max_concurrent: int):
        try:
            self._max_concurrent_post_processing = max(1, int(max_concurrent))
        except Exception:
            self._max_concurrent_post_processing = 1

    def register_websocket(self, websocket, auth_context=None):
        self._websocket_connections[websocket] = {
            "is_admin": bool(getattr(auth_context, "is_admin", False)),
            "user_id": getattr(auth_context, "user_id", None),
        }

    def unregister_websocket(self, websocket):
        self._websocket_connections.pop(websocket, None)

    async def disconnect_user_websockets(self, user_id: int) -> None:
        """Close all open WebSocket connections belonging to a specific user."""
        to_close = [
            ws for ws, ws_auth in list(self._websocket_connections.items())
            if ws_auth.get("user_id") == user_id
        ]
        for ws in to_close:
            self._websocket_connections.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass

    def _track_active_download(self, download_id: int, account_id: Optional[int]) -> None:
        if account_id is None:
            return
        self._active_account_ids[download_id] = account_id
        self._account_active_counts[account_id] = self._account_active_counts.get(account_id, 0) + 1

    def _untrack_active_download(self, download_id: int) -> None:
        account_id = self._active_account_ids.pop(download_id, None)
        if account_id is None:
            return
        remaining = self._account_active_counts.get(account_id, 0) - 1
        if remaining > 0:
            self._account_active_counts[account_id] = remaining
        else:
            self._account_active_counts.pop(account_id, None)

    def _cleanup_completed_tasks(self) -> None:
        completed = [
            did for did, task in self._active_downloads.items()
            if task.done()
        ]
        for did in completed:
            del self._active_downloads[did]
            self._untrack_active_download(did)

    def _cleanup_completed_post_tasks(self) -> None:
        completed = [
            did for did, task in self._active_post.items()
            if task.done()
        ]
        for did in completed:
            del self._active_post[did]

    async def _broadcast_progress(self, download_id: int, progress: float, status: str, **extra):
        """Broadcast progress to all connected WebSocket clients."""
        message = {
            "type": "progress",
            "download_id": download_id,
            "progress": progress,
            "status": status,
            **extra
        }
        snapshot = self._stage_progress.get(download_id, {})
        for key, value in message.items():
            if key in ("type", "download_id"):
                continue
            if value is not None:
                snapshot[key] = value
        self._stage_progress[download_id] = snapshot

        owner_id = await self._resolve_download_owner(download_id)

        dead_connections = set()
        for ws, ws_auth in list(self._websocket_connections.items()):
            try:
                if not ws_auth.get("is_admin"):
                    if owner_id is None or ws_auth.get("user_id") != owner_id:
                        continue
                await ws.send_json(message)
            except Exception:
                dead_connections.add(ws)

        # Clean up dead connections
        for ws in dead_connections:
            self._websocket_connections.pop(ws, None)
        if status in [
            DownloadStatus.COMPLETED.value,
            DownloadStatus.FAILED.value,
            DownloadStatus.CANCELLED.value
        ]:
            self._stage_progress.pop(download_id, None)
            self._download_owners.pop(download_id, None)

    def merge_progress_snapshot(self, data: dict) -> dict:
        """Merge in-memory progress fields into a download dict."""
        snapshot = self._stage_progress.get(data.get("id"))
        if not snapshot:
            return data
        return {**data, **snapshot}

    async def _broadcast_log(self, download_id: int, message: str, level: str = "info"):
        """Broadcast a log line to all connected WebSocket clients."""
        payload = {
            "type": "log",
            "download_id": download_id,
            "level": level,
            "message": message,
        }

        owner_id = await self._resolve_download_owner(download_id)
        dead_connections = set()
        for ws, ws_auth in list(self._websocket_connections.items()):
            try:
                if not ws_auth.get("is_admin"):
                    if owner_id is None or ws_auth.get("user_id") != owner_id:
                        continue
                await ws.send_json(payload)
            except Exception:
                dead_connections.add(ws)

        for ws in dead_connections:
            self._websocket_connections.pop(ws, None)
        await backend_log_stream.emit(
            source="download",
            message=message,
            level=level,
            download_id=download_id
        )

    async def _update_processing(
        self,
        session: AsyncSession,
        download_id: int,
        progress: float,
        message: Optional[str] = None,
        indeterminate: bool = False,
        **extra
    ):
        """Update processing status/progress and broadcast to clients."""
        await session.execute(
            update(Download)
            .where(Download.id == download_id)
            .values(
                status=DownloadStatus.PROCESSING.value,
                progress=progress
            )
        )
        await session.commit()

        await self._broadcast_progress(
            download_id,
            progress,
            DownloadStatus.PROCESSING.value,
            message=message,
            indeterminate=indeterminate,
            **extra
        )

    async def find_active_output_conflicts(self, session: AsyncSession, output_paths: list) -> list:
        """Return the subset of output_paths that already have an active download.

        Active means PENDING, DOWNLOADING or PROCESSING — i.e. another task is
        (or will be) writing to that output file.
        """
        if not output_paths:
            return []
        _active = [
            DownloadStatus.PENDING.value,
            DownloadStatus.DOWNLOADING.value,
            DownloadStatus.PROCESSING.value,
        ]
        result = await session.execute(
            select(Download.output_path).where(
                Download.output_path.in_(output_paths),
                Download.status.in_(_active),
            )
        )
        return list(result.scalars().all())

    async def _stage_download(self, session: AsyncSession, download: Download) -> None:
        """Dedup-check and add the download row on *session* without committing."""
        conflicts = await self.find_active_output_conflicts(session, [download.output_path])
        if conflicts:
            raise ValueError(
                f"A download is already active for this output file: "
                f"{os.path.basename(download.output_path)}"
            )
        session.add(download)
        await session.flush()

    async def queue_download(self, download: Download, session: Optional[AsyncSession] = None) -> Download:
        """Add a download to the queue.

        Without *session*, the row is committed on its own session and enqueued
        immediately.

        With *session*, the row is only staged (added and flushed) on that
        session so the caller can commit it atomically with its own changes.
        The caller must call enqueue_persisted() after a successful commit.
        """
        if session is not None:
            await self._stage_download(session, download)
            return download

        async with async_session_maker() as own_session:
            await self._stage_download(own_session, download)
            await own_session.commit()
            await own_session.refresh(download)
        return await self.enqueue_persisted(download)

    async def enqueue_persisted(self, download: Download) -> Download:
        """Enqueue a download whose DB row has already been committed."""
        self._download_owners[download.id] = download.requested_by_user_id
        await self._queue.put(download.id)
        await self._broadcast_log(
            download.id,
            f"Queued download: {download.program_title} ({download.channel_name})."
        )
        return download

    def _needs_post_processing(self, download: Download, settings: Optional[AppSettings]) -> bool:
        if download.is_vod:
            return False
        if not settings:
            return False
        return settings.transcode_enabled or settings.comskip_enabled

    def _processed_output_candidates(self, input_path: str, settings: Optional[AppSettings]) -> list[str]:
        input_file = Path(input_path)
        candidates: list[Path] = []

        if settings:
            fmt = (getattr(settings, "transcode_format", None) or "mkv").strip().lower()
            candidates.append(input_file.with_suffix(f".{fmt}"))

        return [str(c) for c in candidates]

    def _path_is_under(self, path: str, parent: str) -> bool:
        try:
            path_real = os.path.realpath(os.path.abspath(path))
            parent_real = os.path.realpath(os.path.abspath(parent))
            common = os.path.commonpath([path_real, parent_real])
            return os.path.abspath(common) == os.path.abspath(parent_real)
        except Exception:
            return False

    def _folders_equal(self, folder_a: str, folder_b: str) -> bool:
        try:
            return (
                os.path.realpath(os.path.abspath(folder_a))
                == os.path.realpath(os.path.abspath(folder_b))
            )
        except Exception:
            return False

    async def process_queue(self):
        """Main loop for processing download queue."""
        self._running = True

        while self._running:
            try:
                self._cleanup_completed_tasks()
                # Wait for active downloads to have space
                while len(self._active_downloads) >= self._max_concurrent:
                    await asyncio.sleep(0.5)
                    self._cleanup_completed_tasks()

                # Get next download from queue
                try:
                    download_id = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Skip if cancelled
                if download_id in self._cancelled:
                    self._cancelled.discard(download_id)
                    continue

                self._cleanup_completed_tasks()

                async with async_session_maker() as session:
                    result = await session.execute(
                        select(Download).where(Download.id == download_id)
                    )
                    download = result.scalar_one_or_none()

                    if not download:
                        continue
                    if download.status != DownloadStatus.PENDING.value:
                        continue

                    account_id = download.account_id
                    max_connections = None
                    account_result = await session.execute(
                        select(XtreamAccount.max_connections).where(
                            XtreamAccount.id == account_id
                        )
                    )
                    max_connections = account_result.scalar_one_or_none()

                if max_connections is not None and max_connections > 0:
                    active_for_account = self._account_active_counts.get(account_id, 0)
                    if active_for_account >= max_connections:
                        await self._queue.put(download_id)
                        await asyncio.sleep(0.5)
                        continue

                # Start download task
                task = asyncio.create_task(self._execute_download(download_id))
                self._active_downloads[download_id] = task
                self._track_active_download(download_id, account_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._should_log_loop_error("download_queue"):
                    logger.exception("Error in queue processor")
                    await backend_log_stream.emit(
                        source="download",
                        level="error",
                        message=f"Queue processor error: {e}",
                    )
                await asyncio.sleep(1)

    async def process_post_queue(self):
        """Main loop for processing post-processing queue."""
        self._running = True

        while self._running:
            try:
                self._cleanup_completed_post_tasks()
                while len(self._active_post) >= self._max_concurrent_post_processing:
                    await asyncio.sleep(0.5)
                    self._cleanup_completed_post_tasks()

                try:
                    download_id = await asyncio.wait_for(
                        self._post_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if download_id in self._cancelled:
                    self._cancelled.discard(download_id)
                    await self._delete_cancelled_post_file(download_id)
                    continue

                self._cleanup_completed_post_tasks()

                task = asyncio.create_task(self._execute_post_process(download_id))
                self._active_post[download_id] = task

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._should_log_loop_error("post_queue"):
                    logger.exception("Error in post-processing queue processor")
                    await backend_log_stream.emit(
                        source="download",
                        level="error",
                        message=f"Post-processing queue error: {e}",
                    )
                await asyncio.sleep(1)

    async def recover_incomplete_downloads(self) -> int:
        """
        Recover downloads left mid-flight after a restart.

        - pending: requeue to download queue
        - downloading: restart from scratch (no HTTP resume currently); if complete, continue to processing/finalize
        - processing: requeue to post-processing if input exists; if missing, mark failed (or finalize if output exists)
        """
        async with async_session_maker() as session:
            settings_result = await session.execute(select(AppSettings))
            settings = settings_result.scalar_one_or_none()
            if settings:
                if getattr(settings, "max_concurrent_downloads", None) is not None:
                    try:
                        self._max_concurrent = max(1, int(settings.max_concurrent_downloads))
                    except Exception:
                        pass
                if getattr(settings, "max_concurrent_post_processing", None) is not None:
                    try:
                        self._max_concurrent_post_processing = max(1, int(settings.max_concurrent_post_processing))
                    except Exception:
                        pass
            completed_folder = self._resolve_completed_folder(settings)
            download_folder = self._resolve_download_folder(settings)
            # When both folders are the same directory, "the file is under the
            # completed folder" says nothing about whether post-processing ran:
            # the raw downloaded input lives there too.
            folders_equal = self._folders_equal(download_folder, completed_folder)

            result = await session.execute(
                select(Download).where(
                    Download.status.in_([
                        DownloadStatus.PENDING.value,
                        DownloadStatus.DOWNLOADING.value,
                        DownloadStatus.PROCESSING.value
                    ])
                ).order_by(Download.created_at)
            )
            downloads = list(result.scalars().all())
            if not downloads:
                return 0

            recovered_download_ids: list[int] = []
            recovered_post_ids: list[int] = []

            for download in downloads:
                input_path = download.output_path
                input_file = Path(input_path)

                if download.status == DownloadStatus.PENDING.value:
                    recovered_download_ids.append(download.id)
                    continue

                if download.status == DownloadStatus.DOWNLOADING.value:
                    try:
                        if input_file.is_file() and download.file_size:
                            is_complete = download.downloaded_bytes >= int(download.file_size)
                        elif (
                            input_file.is_file()
                            and download.file_size == 0
                            and download.downloaded_bytes > 0
                        ):
                            # Chunked stream (no Content-Length). The final
                            # post-stream commit records downloaded_bytes with
                            # progress=100; periodic mid-stream commits keep
                            # progress at 0, so a crash right after one of them
                            # is not mistaken for a finished download.
                            is_complete = (
                                input_file.stat().st_size == download.downloaded_bytes
                                and bool(download.progress)
                            )
                        else:
                            is_complete = False
                    except Exception:
                        is_complete = False

                    if is_complete:
                        if self._needs_post_processing(download, settings):
                            download.status = DownloadStatus.PROCESSING.value
                            download.progress = 0.0
                            recovered_post_ids.append(download.id)
                            await self._broadcast_progress(
                                download.id,
                                0.0,
                                DownloadStatus.PROCESSING.value,
                                message="Queued for post-processing...",
                                indeterminate=False,
                                download_progress=100.0,
                            )
                            await self._broadcast_log(download.id, "Recovered after restart: queued for post-processing.")
                        else:
                            completed_path = await self._move_to_completed_async(str(input_file), completed_folder, download_folder)
                            download.output_path = completed_path
                            download.status = DownloadStatus.COMPLETED.value
                            download.progress = 100.0
                            if not download.completed_at:
                                download.completed_at = datetime.utcnow()
                            download.error_message = None
                            await self._sync_schedule_status(session, download.id, DownloadStatus.COMPLETED.value)
                            await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    download.status = DownloadStatus.PENDING.value
                    download.progress = 0.0
                    if download.file_size == 0 and input_file.is_file():
                        try:
                            download.downloaded_bytes = input_file.stat().st_size
                        except Exception:
                            download.downloaded_bytes = 0
                    else:
                        download.downloaded_bytes = 0
                    download.file_size = 0
                    download.error_message = None
                    recovered_download_ids.append(download.id)
                    await self._broadcast_log(download.id, "Recovered after restart: requeued for download.")
                    continue

                if download.status == DownloadStatus.PROCESSING.value:
                    # Only trust "file already in the completed folder" as proof
                    # that post-processing finished when the completed folder is
                    # distinct from the download folder. With equal folders the
                    # raw input sits there before processing has run.
                    if (
                        input_file.is_file()
                        and self._path_is_under(str(input_file), completed_folder)
                        and not (folders_equal and self._needs_post_processing(download, settings))
                    ):
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        if not download.completed_at:
                            download.completed_at = datetime.utcnow()
                        download.error_message = None
                        await self._sync_schedule_status(session, download.id, DownloadStatus.COMPLETED.value)
                        await self._broadcast_log(download.id, "Recovered after restart: marked completed.")
                        continue

                    if input_file.is_file():
                        if self._needs_post_processing(download, settings):
                            recovered_post_ids.append(download.id)
                            await self._broadcast_progress(
                                download.id,
                                0.0,
                                DownloadStatus.PROCESSING.value,
                                message="Queued for post-processing...",
                                indeterminate=False,
                                download_progress=100.0,
                            )
                            await self._broadcast_log(download.id, "Recovered after restart: queued for post-processing.")
                        else:
                            completed_path = await self._move_to_completed_async(str(input_file), completed_folder, download_folder)
                            download.output_path = completed_path
                            download.status = DownloadStatus.COMPLETED.value
                            download.progress = 100.0
                            if not download.completed_at:
                                download.completed_at = datetime.utcnow()
                            download.error_message = None
                            await self._sync_schedule_status(session, download.id, DownloadStatus.COMPLETED.value)
                            await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    # If we lost the input file but still have an output, finalize it.
                    processed_found = None
                    for candidate in self._processed_output_candidates(input_path, settings):
                        if Path(candidate).is_file():
                            processed_found = candidate
                            break
                    if processed_found:
                        completed_path = await self._move_to_completed_async(processed_found, completed_folder, download_folder)
                        download.output_path = completed_path
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        if not download.completed_at:
                            download.completed_at = datetime.utcnow()
                        download.error_message = None
                        await self._sync_schedule_status(session, download.id, DownloadStatus.COMPLETED.value)
                        await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    download.status = DownloadStatus.FAILED.value
                    if not download.completed_at:
                        download.completed_at = datetime.utcnow()
                    download.error_message = "Recovery failed: missing input file for post-processing."
                    await self._sync_schedule_status(session, download.id, DownloadStatus.FAILED.value)
                    await self._broadcast_log(download.id, download.error_message, level="error")

            if recovered_download_ids:
                min_free_gb = (
                    settings.min_free_space_gb
                    if settings and settings.min_free_space_gb is not None
                    else 25
                ) or 0
                if min_free_gb > 0 and await asyncio.to_thread(os.path.exists, download_folder):
                    usage = await asyncio.to_thread(shutil.disk_usage, download_folder)
                    free_gb = usage.free / (1024 ** 3)
                    if free_gb < min_free_gb:
                        download_by_id = {d.id: d for d in downloads}
                        for dl_id in list(recovered_download_ids):
                            dl = download_by_id.get(dl_id)
                            if dl:
                                dl.status = DownloadStatus.FAILED.value
                                dl.error_message = (
                                    f"Disk space below minimum at startup "
                                    f"({free_gb:.1f} GB free, {min_free_gb} GB required)."
                                )
                                if not dl.completed_at:
                                    dl.completed_at = datetime.utcnow()
                                await self._sync_schedule_status(session, dl.id, DownloadStatus.FAILED.value)
                                await self._broadcast_log(
                                    dl_id,
                                    dl.error_message,
                                    level="error",
                                )
                        recovered_download_ids.clear()

            await session.commit()

        for download_id in recovered_download_ids:
            await self._queue.put(download_id)
        for download_id in recovered_post_ids:
            await self._post_queue.put(download_id)
        return len(recovered_download_ids) + len(recovered_post_ids)

    async def _execute_download(self, download_id: int):
        """Execute a single download."""
        # These survive into the except handlers: once the full payload has
        # been written to disk (and possibly moved to the completed folder),
        # cancel/error handling must never destroy a complete recording.
        transfer_complete = False
        moved_completed_path: Optional[str] = None
        settings = None
        async with async_session_maker() as session:
            download = None
            try:
                # Get download record
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()

                if not download:
                    return

                if download.status != DownloadStatus.PENDING.value:
                    return

                # Skip terminal states
                if download.status in [
                    DownloadStatus.COMPLETED.value,
                    DownloadStatus.FAILED.value,
                    DownloadStatus.CANCELLED.value,
                ]:
                    return

                # Check if cancelled
                if download_id in self._cancelled:
                    self._cancelled.discard(download_id)
                    return

                # Ensure output directory exists
                output_dir = os.path.dirname(download.output_path)
                os.makedirs(output_dir, exist_ok=True)

                # Preserve any recovered partial-file offset for Range resume attempt.
                recovery_offset = download.downloaded_bytes or 0

                # Update status to downloading
                download.status = DownloadStatus.DOWNLOADING.value
                download.progress = 0.0
                download.downloaded_bytes = 0
                await session.commit()

                await self._broadcast_progress(
                    download_id, 0, DownloadStatus.DOWNLOADING.value
                )
                await self._broadcast_log(
                    download_id,
                    f"Download started: {os.path.basename(download.output_path)}"
                )

                # Start download
                downloaded_bytes = await self._download_file(
                    download.source_url,
                    download.output_path,
                    download_id,
                    session,
                    offset=recovery_offset,
                )
                if downloaded_bytes == 0:
                    raise Exception(
                        "Provider returned an empty response. "
                        "The catchup window for this program may have expired."
                    )
                transfer_complete = True
                await self._broadcast_log(download_id, "Download transfer complete.")

                settings_result = await session.execute(select(AppSettings))
                settings = settings_result.scalar_one_or_none()

                if self._needs_post_processing(download, settings):
                    download.status = DownloadStatus.PROCESSING.value
                    download.progress = 0.0
                    await session.commit()

                    await self._broadcast_progress(
                        download_id,
                        0.0,
                        DownloadStatus.PROCESSING.value,
                        message="Queued for post-processing...",
                        indeterminate=False,
                        download_progress=100.0,
                    )
                    await self._broadcast_log(download_id, "Queued for post-processing.")
                    await self._post_queue.put(download_id)
                    return

                completed_folder = self._resolve_completed_folder(settings)
                download_folder = self._resolve_download_folder(settings)
                try:
                    completed_path = await self._move_to_completed_async(download.output_path, completed_folder, download_folder)
                except OSError as move_err:
                    # Move failed (e.g. ENOSPC on the completed folder mount). The
                    # downloaded file is still intact in the download folder. Mark as
                    # FAILED without deleting the source.
                    if move_err.errno == errno.ENOSPC:
                        msg = (
                            "Not enough space in the completed recordings folder. "
                            "Your recording is safe in the downloads folder. "
                            "Free up space on that drive."
                        )
                    else:
                        msg = _friendly_error(move_err)
                    download.status = DownloadStatus.FAILED.value
                    if not download.completed_at:
                        download.completed_at = datetime.utcnow()
                    download.error_message = msg
                    await self._sync_schedule_status(session, download_id, DownloadStatus.FAILED.value)
                    try:
                        await session.commit()
                    except Exception:
                        pass
                    await self._broadcast_progress(
                        download_id,
                        download.progress,
                        DownloadStatus.FAILED.value,
                        error=msg,
                    )
                    await self._broadcast_log(download_id, f"Download failed: {msg}", level="error")
                    return
                download.output_path = completed_path
                moved_completed_path = completed_path

                await self._store_recorded_duration(download, completed_path)
                integrity_warning = await self._integrity_check_warning(completed_path, settings)
                download.status = DownloadStatus.COMPLETED.value
                download.progress = 100.0
                download.completed_at = datetime.utcnow()
                download.error_message = (
                    f"Completed with warnings: {integrity_warning}" if integrity_warning else None
                )
                await self._sync_schedule_status(session, download_id, DownloadStatus.COMPLETED.value)
                await session.commit()

                await self._broadcast_progress(
                    download_id, 100, DownloadStatus.COMPLETED.value
                )
                await self._broadcast_log(
                    download_id,
                    f"Download completed: {os.path.basename(completed_path)}"
                )
                if integrity_warning:
                    await self._broadcast_log(
                        download_id,
                        f"Completed with warnings: {integrity_warning}",
                        level="warning",
                    )
                await self._trigger_plex_refresh(completed_path)

            except asyncio.CancelledError:
                if download is None:
                    result = await session.execute(
                        select(Download).where(Download.id == download_id)
                    )
                    download = result.scalar_one_or_none()
                completed_path = moved_completed_path
                if (
                    completed_path is None
                    and transfer_complete
                    and download is not None
                    and download.output_path
                    and os.path.exists(download.output_path)
                ):
                    # The final byte was already written before the cancel
                    # arrived: the recording is complete. Move it to the
                    # completed folder instead of deleting it.
                    if settings is None:
                        settings = await self._load_app_settings()
                    try:
                        completed_path = await self._move_to_completed_async(
                            download.output_path,
                            self._resolve_completed_folder(settings),
                            self._resolve_download_folder(settings),
                        )
                    except OSError:
                        # Move failed; keep the recording where it is.
                        completed_path = download.output_path
                if download is not None and completed_path is not None:
                    # Cancel arrived after the download already finished (and
                    # possibly after the move). Treat the recording as complete
                    # and leave the file untouched.
                    self._cancelled.discard(download_id)
                    await self._finalize_completed_after_interrupt(
                        session, download_id, completed_path
                    )
                    await self._broadcast_log(
                        download_id,
                        f"Download completed: {os.path.basename(completed_path)}"
                    )
                    await self._trigger_plex_refresh(completed_path)
                    return
                if download:
                    download.status = DownloadStatus.CANCELLED.value
                    await self._sync_schedule_status(session, download_id, DownloadStatus.CANCELLED.value)
                    await session.commit()
                    if download.output_path and os.path.exists(download.output_path):
                        try:
                            os.unlink(download.output_path)
                        except OSError:
                            pass
                await self._broadcast_progress(
                    download_id, download.progress if download else 0, DownloadStatus.CANCELLED.value
                )
                await self._broadcast_log(download_id, "Download cancelled.", level="warning")

            except Exception as e:
                if moved_completed_path is not None:
                    # The file already reached the completed folder. A late
                    # failure (e.g. the final DB commit) must never delete or
                    # fail a recording that survived on disk. This also covers
                    # a failed commit leaving the session pending rollback: the
                    # finalize helper retries on a fresh session.
                    await self._finalize_completed_after_interrupt(
                        session, download_id, moved_completed_path
                    )
                    await self._broadcast_log(
                        download_id,
                        f"Download completed: {os.path.basename(moved_completed_path)} "
                        f"(ignored post-completion error: {e})"
                    )
                    await self._trigger_plex_refresh(moved_completed_path)
                    return
                # Download failed
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()
                if download:
                    download.status = DownloadStatus.FAILED.value
                    if not download.completed_at:
                        download.completed_at = datetime.utcnow()
                    download.error_message = _friendly_error(e)
                    await self._sync_schedule_status(session, download_id, DownloadStatus.FAILED.value)
                    await session.commit()
                    try:
                        if download.output_path and os.path.exists(download.output_path):
                            os.unlink(download.output_path)
                    except OSError:
                        pass

                await self._broadcast_progress(
                    download_id,
                    download.progress if download else 0,
                    DownloadStatus.FAILED.value,
                    error=str(e)
                )
                await self._broadcast_log(download_id, f"Download failed: {e}", level="error")

    async def _execute_post_process(self, download_id: int):
        """Execute post-processing for a downloaded file."""
        # Set once the output file has been moved to the completed folder so
        # cancel/error handling never marks a surviving recording CANCELLED or
        # FAILED (which would expose it to later cleanup/retry deletion).
        moved_completed_path: Optional[str] = None
        async with async_session_maker() as session:
            working_input_path: Optional[str] = None
            completed_output_path: Optional[str] = None
            try:
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()

                if not download:
                    return

                if download.status != DownloadStatus.PROCESSING.value:
                    return

                if download_id in self._cancelled:
                    self._cancelled.discard(download_id)
                    raise asyncio.CancelledError()

                settings_result = await session.execute(select(AppSettings))
                settings = settings_result.scalar_one_or_none()
                completed_folder = self._resolve_completed_folder(settings)
                download_folder = self._resolve_download_folder(settings)

                original_path = download.output_path
                working_input_path = original_path
                original_file = Path(original_path)
                if not original_file.is_file():
                    processed_found = None
                    for candidate in self._processed_output_candidates(original_path, settings):
                        if Path(candidate).is_file():
                            processed_found = candidate
                            break
                    if processed_found:
                        completed_path = await self._move_to_completed_async(processed_found, completed_folder, download_folder)
                        moved_completed_path = completed_path
                        download.output_path = completed_path
                        await self._store_recorded_duration(download, completed_path)
                        integrity_warning = await self._integrity_check_warning(completed_path, settings)
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        download.completed_at = datetime.utcnow()
                        download.error_message = (
                            f"Completed with warnings: {integrity_warning}" if integrity_warning else None
                        )
                        await self._sync_schedule_status(session, download_id, DownloadStatus.COMPLETED.value)
                        await session.commit()
                        await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)
                        await self._broadcast_log(download_id, "Post-processing recovery: finalized completed output.")
                        await self._trigger_plex_refresh(completed_path)
                        return

                    raise Exception("Missing input file for post-processing.")

                if not self._needs_post_processing(download, settings):
                    completed_path = await self._move_to_completed_async(original_path, completed_folder, download_folder)
                    moved_completed_path = completed_path
                    download.output_path = completed_path
                    await self._store_recorded_duration(download, completed_path)
                    integrity_warning = await self._integrity_check_warning(completed_path, settings)
                    download.status = DownloadStatus.COMPLETED.value
                    download.progress = 100.0
                    download.completed_at = datetime.utcnow()
                    download.error_message = (
                        f"Completed with warnings: {integrity_warning}" if integrity_warning else None
                    )
                    await self._sync_schedule_status(session, download_id, DownloadStatus.COMPLETED.value)
                    await session.commit()
                    await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)
                    await self._broadcast_log(download_id, "Post-processing disabled; finalized completed output.")
                    await self._trigger_plex_refresh(completed_path)
                    return

                await self._broadcast_log(download_id, "Post-processing started.")

                final_path, warnings = await self._post_process(
                    original_path,
                    download_id,
                    session,
                    settings
                )

                final_path = self._select_final_path(original_path, final_path)
                completed_path = await self._move_to_completed_async(final_path, completed_folder, download_folder)
                completed_output_path = completed_path
                moved_completed_path = completed_path
                download.output_path = completed_path
                await self._store_recorded_duration(download, completed_path)
                integrity_warning = await self._integrity_check_warning(completed_path, settings)
                if integrity_warning:
                    warnings.append(integrity_warning)
                if warnings:
                    download.error_message = f"Completed with warnings: {'; '.join(warnings)}"
                    await self._broadcast_log(
                        download_id,
                        f"Completed with warnings: {'; '.join(warnings)}",
                        level="warning"
                    )
                else:
                    download.error_message = None

                delete_original = (settings.delete_original_after_transcode if settings is not None else True)
                self._cleanup_working_files(
                    original_path,
                    completed_path,
                    keep_logs=bool(warnings),
                    delete_original=delete_original,
                )

                download.status = DownloadStatus.COMPLETED.value
                download.progress = 100.0
                download.completed_at = datetime.utcnow()
                await self._sync_schedule_status(session, download_id, DownloadStatus.COMPLETED.value)
                await session.commit()

                await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)
                await self._broadcast_log(
                    download_id,
                    f"Post-processing completed: {os.path.basename(completed_path)}"
                )
                await self._trigger_plex_refresh(completed_path)

            except asyncio.CancelledError:
                if moved_completed_path is not None:
                    # File was already moved to the completed folder before the
                    # cancel arrived. Persist COMPLETED so the recording is not
                    # left as PROCESSING/CANCELLED with an orphaned file.
                    self._cancelled.discard(download_id)
                    await self._finalize_completed_after_interrupt(
                        session, download_id, moved_completed_path
                    )
                    return
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()
                if download and download.status in [
                    DownloadStatus.PENDING.value,
                    DownloadStatus.DOWNLOADING.value,
                    DownloadStatus.PROCESSING.value,
                ]:
                    download.status = DownloadStatus.CANCELLED.value
                    await self._sync_schedule_status(session, download_id, DownloadStatus.CANCELLED.value)
                    await session.commit()
                    # Remove Comskip artifacts and partial transcode outputs left
                    # by the cancelled run. The original recording is kept.
                    if working_input_path:
                        self._cleanup_working_files(
                            working_input_path,
                            completed_output_path or working_input_path,
                            keep_logs=False,
                            delete_original=False,
                        )
                    await self._broadcast_progress(
                        download_id, download.progress, DownloadStatus.CANCELLED.value
                    )
                    await self._broadcast_log(download_id, "Post-processing cancelled.", level="warning")

            except Exception as e:
                if moved_completed_path is not None and os.path.exists(moved_completed_path):
                    # Post-processing crashed after the file was already moved
                    # to the completed folder. The artifact survived, so record
                    # the download as COMPLETED instead of FAILED (a FAILED row
                    # pointing at the completed file would let retry/cleanup
                    # overwrite or delete a good recording).
                    await self._finalize_completed_after_interrupt(
                        session,
                        download_id,
                        moved_completed_path,
                        error_message=f"Completed with warnings: {_friendly_error(e)}",
                    )
                    await self._broadcast_log(
                        download_id,
                        f"Post-processing error after the file was finalized ({e}); "
                        f"recording kept: {os.path.basename(moved_completed_path)}",
                        level="warning",
                    )
                    await self._trigger_plex_refresh(moved_completed_path)
                    return
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()
                if download:
                    download.status = DownloadStatus.FAILED.value
                    if not download.completed_at:
                        download.completed_at = datetime.utcnow()
                    download.error_message = _friendly_error(e)
                    await self._sync_schedule_status(session, download_id, DownloadStatus.FAILED.value)
                    await session.commit()

                # Remove Comskip artifacts and partial transcode outputs left by
                # the failed run. Logs are kept for debugging and the original
                # recording stays in the download folder.
                if working_input_path:
                    self._cleanup_working_files(
                        working_input_path,
                        completed_output_path or working_input_path,
                        keep_logs=True,
                        delete_original=False,
                    )

                await self._broadcast_progress(
                    download_id,
                    download.progress if download else 0,
                    DownloadStatus.FAILED.value,
                    error=str(e)
                )
                await self._broadcast_log(download_id, f"Post-processing failed: {e}", level="error")

    async def _delete_cancelled_post_file(self, download_id: int) -> None:
        """Delete the source file for a post-processing job cancelled before it started."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                dl = result.scalar_one_or_none()
                # Never delete a recording that was already finalized: a stale
                # cancel flag must not destroy a COMPLETED file.
                if dl and dl.output_path and dl.status != DownloadStatus.COMPLETED.value:
                    path = Path(dl.output_path)
                    if path.is_file():
                        path.unlink()
        except Exception:
            pass

    async def _load_app_settings(self) -> Optional[AppSettings]:
        """Load AppSettings on a fresh session; returns None when unavailable."""
        try:
            async with async_session_maker() as session:
                result = await session.execute(select(AppSettings))
                return result.scalar_one_or_none()
        except Exception:
            return None

    async def _store_recorded_duration(self, download: Download, file_path: str) -> None:
        """Probe the finished recording with ffprobe and persist its real duration.

        Stores the actual file duration (seconds) on the download row so the UI
        can show how long the recording really is, independent of the EPG
        program duration. Best-effort: a probe failure must never break
        completion, so all errors are swallowed and the column stays NULL.
        """
        try:
            if not file_path or not os.path.isfile(file_path):
                return
            from services.post_processor import post_processor

            duration = await post_processor._get_duration(file_path)
        except Exception:
            return
        if duration and duration > 0:
            download.recorded_duration_seconds = int(round(duration))

    async def _integrity_check_warning(
        self,
        file_path: str,
        settings: Optional[AppSettings],
    ) -> Optional[str]:
        """Run the post-download ffprobe sanity check on the finished file.

        Returns a short warning string when the file looks corrupt, else None.
        The check never fails or deletes the recording: a suspect file is
        surfaced as "Completed with warnings" so the user can inspect it.
        Disabled via AppSettings.integrity_check_enabled (default on).
        """
        if settings is not None and settings.integrity_check_enabled is False:
            return None
        try:
            if not file_path or not os.path.isfile(file_path):
                return None
            from services.post_processor import post_processor

            result = await post_processor.probe_media_integrity(file_path)
        except Exception:
            return None
        if result.get("checked") and not result.get("ok"):
            reason = result.get("reason") or "ffprobe could not validate the file"
            return f"file may be corrupt ({reason})"
        return None

    async def _finalize_completed_after_interrupt(
        self,
        session: AsyncSession,
        download_id: int,
        completed_path: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Persist COMPLETED for a recording whose file already survived on disk.

        Called from cancel/error handlers once the full recording has been
        written (and usually moved to the completed folder). The file must
        never be lost just because the caller's session is unusable (e.g. a
        failed commit left it pending rollback), so this retries once on a
        fresh session and never raises a DB error back to the caller.
        """
        values = {
            "status": DownloadStatus.COMPLETED.value,
            "output_path": completed_path,
            "progress": 100.0,
            "completed_at": datetime.utcnow(),
            "error_message": error_message,
        }
        try:
            await session.execute(
                update(Download).where(Download.id == download_id).values(**values)
            )
            await self._sync_schedule_status(session, download_id, DownloadStatus.COMPLETED.value)
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            try:
                async with async_session_maker() as fresh_session:
                    await fresh_session.execute(
                        update(Download).where(Download.id == download_id).values(**values)
                    )
                    await self._sync_schedule_status(
                        fresh_session, download_id, DownloadStatus.COMPLETED.value
                    )
                    await fresh_session.commit()
            except Exception:
                logger.exception(
                    "Could not persist COMPLETED state for download %s; "
                    "file preserved at %s",
                    download_id,
                    completed_path,
                )
        await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)

    async def _resolve_download_owner(self, download_id: int) -> int | None:
        if download_id in self._download_owners:
            return self._download_owners.get(download_id)
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Download.requested_by_user_id).where(Download.id == download_id)
                )
                owner_id = result.scalar_one_or_none()
                self._download_owners[download_id] = owner_id
                return owner_id
        except Exception:
            return None

    async def _trigger_plex_refresh(self, completed_path: str) -> None:
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(PlexServer).where(PlexServer.enabled.is_(True)).limit(1)
                )
                plex_server = result.scalar_one_or_none()
                if not plex_server:
                    return
                section_ids = plex_service.parse_section_ids(plex_server.library_section_ids)
                if not section_ids:
                    return
                token = credential_crypto.decrypt(
                    plex_server.access_token_encrypted or plex_server.token_encrypted
                )
                results = await plex_service.trigger_library_refresh(
                    plex_server.connection_uri or plex_server.base_url,
                    token,
                    section_ids,
                )
                failures = [r for r in results if not r.get("ok")]
                if failures:
                    await backend_log_stream.emit(
                        source="download",
                        level="warning",
                        message=f"Plex refresh had failures for {len(failures)} section(s).",
                    )
        except Exception as exc:
            await backend_log_stream.emit(
                source="download",
                level="warning",
                message=f"Plex refresh skipped: {exc}",
            )

    async def _sync_schedule_status(self, session: AsyncSession, download_id: int, new_status: str) -> None:
        """Update the linked ScheduledRecording when a download reaches a terminal state."""
        result = await session.execute(
            select(ScheduledRecording).where(ScheduledRecording.download_id == download_id)
        )
        schedule = result.scalar_one_or_none()
        _protected = {ScheduledStatus.CANCELLED.value, ScheduledStatus.COMPLETED.value}
        if schedule and schedule.status != new_status and schedule.status not in _protected:
            schedule.status = new_status
            schedule.status_message = None
            schedule.updated_at = datetime.utcnow()

    def _resolve_completed_folder(self, settings: Optional[AppSettings]) -> str:
        if settings and settings.completed_folder and not settings.completed_folder.startswith("./data"):
            if is_docker_env() and not settings.completed_folder.startswith("/app/"):
                return app_settings.default_completed_folder
            if not is_docker_env() and settings.completed_folder.startswith("/app/"):
                return app_settings.default_completed_folder
            return settings.completed_folder
        return app_settings.default_completed_folder

    def _resolve_download_folder(self, settings: Optional[AppSettings]) -> str:
        if settings and settings.download_folder and not settings.download_folder.startswith("./data"):
            if is_docker_env() and not settings.download_folder.startswith("/app/"):
                return app_settings.default_download_folder
            if not is_docker_env() and settings.download_folder.startswith("/app/"):
                return app_settings.default_download_folder
            return settings.download_folder
        return app_settings.default_download_folder

    def _select_final_path(self, original_path: str, final_path: str) -> str:
        if final_path and os.path.exists(final_path):
            return final_path
        if os.path.exists(original_path):
            return original_path
        raise Exception("No output file available to move to completed folder.")

    async def _move_to_completed_async(
        self, path: str, completed_folder: str, download_folder: Optional[str] = None
    ) -> str:
        """Run _move_to_completed off the event loop.

        A multi-GB shutil.move across filesystems (e.g. Docker volume to NAS)
        is a synchronous copy that would otherwise freeze every download and
        WebSocket update until it finishes.
        """
        return await asyncio.to_thread(
            self._move_to_completed, path, completed_folder, download_folder
        )

    def _move_to_completed(self, path: str, completed_folder: str, download_folder: Optional[str] = None) -> str:
        if download_folder:
            try:
                common = os.path.commonpath([os.path.abspath(path), os.path.abspath(download_folder)])
            except Exception:
                common = None
            if common and os.path.abspath(common) == os.path.abspath(download_folder):
                rel = os.path.relpath(path, download_folder)
                dest = os.path.join(completed_folder, rel)
            else:
                dest = os.path.join(completed_folder, os.path.basename(path))
        else:
            dest = os.path.join(completed_folder, os.path.basename(path))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(path) == os.path.abspath(dest):
            return path
        try:
            shutil.move(path, dest)
        except OSError:
            try:
                if os.path.exists(dest):
                    os.unlink(dest)
            except OSError:
                pass
            raise
        if download_folder:
            # Series episodes live in Show/Season subfolders of the download
            # folder; remove any now-empty parents so they don't accumulate.
            self._prune_empty_parent_dirs(os.path.dirname(path), download_folder)
        return dest

    def _prune_empty_parent_dirs(self, start_dir: str, root: str) -> None:
        """Remove empty directories from *start_dir* up to, but never including, *root*."""
        try:
            root_real = os.path.realpath(os.path.abspath(root))
            current = os.path.realpath(os.path.abspath(start_dir))
            while current != root_real and self._path_is_under(current, root):
                try:
                    if not os.path.isdir(current) or os.listdir(current):
                        break
                    os.rmdir(current)
                except OSError:
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
        except Exception:
            pass

    def _cleanup_working_files(self, original_path: str, completed_path: str, keep_logs: bool, delete_original: bool = True) -> None:
        try:
            original_file = Path(original_path)
            base_dir = original_file.parent
            stem = original_file.stem
            # Escape glob special chars ([, ], ?) in the stem so channel names like
            # "[BBC HD]" don't silently fail to match or accidentally match unrelated files.
            escaped_stem = glob_module.escape(stem)
            completed_real = os.path.realpath(os.path.abspath(completed_path))

            if delete_original and original_path != completed_path and original_file.exists():
                original_file.unlink()

            # Only remove known ComSkip/FFmpeg working files.
            # .xml/.srt/.ass/.vtt are user subtitle/metadata files, never ComSkip outputs.
            patterns = [
                f"{escaped_stem}_seg*.ts",
                f"{escaped_stem}.concat.txt",
                f"{escaped_stem}.edl",
                f"{escaped_stem}.txt",
                f"{escaped_stem}.logo",
                f"{escaped_stem}.csv",
                f"{escaped_stem}.vdr",
                # Partial transcode outputs left by a mid-transcode FFmpeg failure.
                # The completed_real guard below skips these when transcode succeeded and
                # the file was already moved to completed_folder.
                f"{escaped_stem}.mkv",
                f"{escaped_stem}.mp4",
            ]
            if not keep_logs:
                patterns.extend([
                    f"{escaped_stem}.log",
                    f"{escaped_stem}.*.ffmpeg.log",
                ])

            for pattern in patterns:
                for path in base_dir.glob(pattern):
                    try:
                        candidate_real = os.path.realpath(os.path.abspath(str(path)))
                        if candidate_real == completed_real:
                            continue
                        path.unlink()
                    except Exception:
                        continue
        except Exception:
            pass

    async def _preflight_disk_space(
        self,
        session: AsyncSession,
        output_path: str,
        total_size: int,
        already_have: int,
        download_id: int,
    ) -> None:
        """Fail fast when the remaining bytes (per Content-Length) cannot fit.

        Only runs when the provider reported a Content-Length. The remaining
        bytes must fit in the free space of the download folder while keeping
        the configured minimum free space (AppSettings.min_free_space_gb).
        """
        if total_size <= 0:
            return
        remaining = total_size - already_have
        if remaining <= 0:
            return
        folder = os.path.dirname(output_path) or "."
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, folder)
            free_bytes = usage.free
        except OSError:
            # Cannot stat the folder; let the download proceed and fail
            # naturally (e.g. ENOSPC) if space truly runs out.
            return
        min_free_bytes = 0
        try:
            result = await session.execute(select(AppSettings))
            row = result.scalar_one_or_none()
            if row is None:
                # No settings row yet: same default as services.disk_space.
                min_free_bytes = int(MIN_FREE_SPACE_FALLBACK_GB * (1024 ** 3))
            elif isinstance(row, AppSettings):
                value = row.min_free_space_gb
                min_free_gb = MIN_FREE_SPACE_FALLBACK_GB if value is None else value
                min_free_bytes = int(float(min_free_gb) * (1024 ** 3))
            # Anything else (e.g. a test double that is not an AppSettings row)
            # keeps min_free_bytes at 0: the Content-Length check still applies.
        except Exception:
            min_free_bytes = 0
        if free_bytes < remaining + min_free_bytes:
            raise Exception(
                f"Not enough disk space for this recording: "
                f"{remaining / (1024 ** 3):.2f} GB still to download, "
                f"only {free_bytes / (1024 ** 3):.2f} GB free "
                f"({min_free_bytes / (1024 ** 3):.0f} GB minimum free space must be kept). "
                f"Free up space and retry."
            )

    async def _stream_response_to_file(
        self,
        response,
        output_path: str,
        file_mode: str,
        downloaded_start: int,
        total_size: int,
        download_id: int,
        session: AsyncSession,
    ) -> int:
        """Write the body of *response* to *output_path* and return total bytes accumulated.

        *downloaded_start* is added to the byte counter before the first write so
        a resumed download reports cumulative progress from the start of the file.
        """
        if total_size > 0:
            await self._broadcast_log(
                download_id,
                f"HTTP {response.status}. Expected size: {total_size:,} bytes."
            )
        else:
            await self._broadcast_log(
                download_id,
                f"HTTP {response.status}. Size unknown; streaming download."
            )

        await self._preflight_disk_space(
            session, output_path, total_size, downloaded_start, download_id
        )

        downloaded = downloaded_start
        last_progress_update = 0
        last_bytes_update = downloaded_start

        async with aiofiles.open(output_path, file_mode) as f:
            async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                if download_id in self._cancelled:
                    raise asyncio.CancelledError()

                await f.write(chunk)
                downloaded += len(chunk)

                if total_size > 0:
                    progress = (downloaded / total_size) * 100
                    should_update = (
                        progress - last_progress_update >= 1
                        or downloaded == total_size
                    )
                else:
                    # Chunked transfer: no Content-Length, so percent progress
                    # is unknown. Emit on a byte cadence instead so the DB and
                    # UI still see the transfer advancing.
                    progress = 0
                    should_update = (
                        downloaded - last_bytes_update
                        >= CHUNKED_PROGRESS_INTERVAL_BYTES
                    )

                if should_update:
                    last_progress_update = progress
                    last_bytes_update = downloaded

                    await session.execute(
                        update(Download)
                        .where(Download.id == download_id)
                        .values(
                            progress=progress,
                            downloaded_bytes=downloaded,
                            file_size=total_size
                        )
                    )
                    await session.commit()

                    extra = {"indeterminate": True} if total_size == 0 else {}
                    await self._broadcast_progress(
                        download_id,
                        progress,
                        DownloadStatus.DOWNLOADING.value,
                        downloaded_bytes=downloaded,
                        file_size=total_size,
                        download_progress=progress,
                        **extra
                    )

        # For chunked streams (total_size == 0), commit the final byte count with
        # progress=100 so recovery after a crash can detect a complete file.
        # Periodic mid-stream commits above keep progress at 0, so only this
        # final commit marks the stream as fully transferred.
        if total_size == 0:
            await session.execute(
                update(Download)
                .where(Download.id == download_id)
                .values(downloaded_bytes=downloaded, progress=100.0)
            )
            await session.commit()

        await self._broadcast_log(
            download_id,
            f"Download bytes written: {downloaded:,}."
        )
        return downloaded

    async def _download_file(
        self,
        url: str,
        output_path: str,
        download_id: int,
        session: AsyncSession,
        offset: int = 0,
    ):
        """Download with bounded retries on transient network errors.

        A connection reset or read timeout mid-transfer used to fail the
        download permanently. Each retry recomputes the resume offset from the
        bytes already on disk; _download_file_once then resumes via HTTP Range
        when the provider supports it, or restarts from byte 0 otherwise.
        Non-network errors (HTTP status errors, disk errors) propagate
        immediately.
        """
        attempt = 0
        current_offset = offset
        while True:
            try:
                return await self._download_file_once(
                    url, output_path, download_id, session, offset=current_offset
                )
            except Exception as e:
                if not _is_transient_network_error(e) or attempt >= TRANSIENT_RETRY_LIMIT:
                    raise
                attempt += 1
                try:
                    current_offset = os.path.getsize(output_path)
                except OSError:
                    current_offset = 0
                delay = TRANSIENT_RETRY_BACKOFF_SECONDS * attempt
                await self._broadcast_log(
                    download_id,
                    f"Network error during download: {type(e).__name__}: {e}. "
                    f"Retrying in {delay:.0f}s from byte {current_offset:,} "
                    f"(attempt {attempt}/{TRANSIENT_RETRY_LIMIT}).",
                    level="warning",
                )
                await asyncio.sleep(delay)

    async def _download_file_once(
        self,
        url: str,
        output_path: str,
        download_id: int,
        session: AsyncSession,
        offset: int = 0,
    ):
        """Stream download a file with progress tracking.

        If *offset* is non-zero a ``Range: bytes=<offset>-`` header is sent.
        A 206 response with a matching Content-Range is resumed (``ab`` mode).
        A 200 response (server ignored Range) re-downloads from byte 0 (``wb``).
        A 206 whose start position cannot be confirmed (absent or mismatched
        Content-Range) releases the connection and issues a fresh GET without the
        Range header so the written file always starts from a known byte 0.
        """
        timeout = aiohttp.ClientTimeout(total=None, sock_read=60)

        request_headers: dict = {}
        if offset > 0:
            request_headers["Range"] = f"bytes={offset}-"

        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            needs_restart = False

            async with http_session.get(url, headers=request_headers) as response:
                if response.status == 416 and offset > 0:
                    # 416 means the Range start is past the resource end.
                    # Parse Content-Range: bytes */TOTAL to find the real remote size.
                    remote_total = None
                    cr_header = response.headers.get("Content-Range", "")
                    if cr_header.startswith("bytes */"):
                        try:
                            remote_total = int(cr_header[len("bytes */"):].strip())
                        except ValueError:
                            pass
                    if remote_total is not None and remote_total < offset:
                        # Local file is larger than the remote resource: stale or
                        # corrupt partial. Re-download from byte 0.
                        needs_restart = True
                        await self._broadcast_log(
                            download_id,
                            f"Provider returned 416; local file ({offset} B) exceeds "
                            f"remote size ({remote_total} B). Re-downloading from start.",
                        )
                    else:
                        # offset == remote_total or no Content-Range: file is complete.
                        await self._broadcast_log(
                            download_id,
                            "Provider returned 416 (Range Not Satisfiable); "
                            "file already complete on disk.",
                        )
                        return offset
                if needs_restart:
                    pass  # fall through to the re-request block below
                elif response.status not in (200, 206):
                    raise Exception(f"HTTP {response.status}: {response.reason}")

                content_type = response.headers.get("Content-Type", "")
                if not needs_restart and content_type.lower().startswith("text/"):
                    raise Exception(
                        f"Provider returned an error page (Content-Type: {content_type}). "
                        "The catchup window may have expired or be unavailable."
                    )

                total_size = response.content_length or 0
                range_start = None
                if response.status == 206:
                    content_range = response.headers.get("Content-Range")
                    if content_range:
                        # Parse start byte from "bytes <start>-<end>/<total>"
                        try:
                            byte_range = content_range.split("/")[0].strip()
                            if byte_range.lower().startswith("bytes "):
                                byte_range = byte_range[6:]
                            start_str = byte_range.split("-")[0].strip()
                            if start_str.isdigit():
                                range_start = int(start_str)
                        except Exception:
                            pass
                        if "/" in content_range:
                            total_part = content_range.split("/")[-1].strip()
                            if total_part.isdigit():
                                total_size = int(total_part)

                # Only resume if server confirmed it started at the requested offset.
                # If Content-Range is present but starts elsewhere, the provider silently
                # returned data from a different position; appending would corrupt the file.
                range_mismatch = range_start is not None and range_start != offset
                resuming = (
                    response.status == 206
                    and offset > 0
                    and range_start is not None
                    and range_start == offset
                )

                if needs_restart:
                    pass  # 416 + oversized local file: skip streaming, re-request below
                elif response.status == 206 and offset > 0 and not resuming:
                    # The 206 body starts at an unknown or wrong offset, not byte 0.
                    # Writing it with 'wb' would produce a silently truncated file.
                    # Release this connection without reading the body, then re-request
                    # from byte 0 with no Range header.
                    needs_restart = True
                    if range_mismatch:
                        await self._broadcast_log(
                            download_id,
                            f"Provider returned Content-Range start {range_start:,} "
                            f"instead of requested {offset:,}; re-requesting from start."
                        )
                    else:
                        await self._broadcast_log(
                            download_id,
                            "Provider returned 206 without Content-Range; re-requesting from start."
                        )
                else:
                    if resuming:
                        await self._broadcast_log(
                            download_id,
                            f"Resuming from byte {offset:,}."
                        )
                        return await self._stream_response_to_file(
                            response, output_path, "ab", offset,
                            total_size, download_id, session
                        )
                    else:
                        if offset > 0:
                            # Provider ignored the Range request and returned the
                            # full resource. If its Content-Length shows the file
                            # we already have on disk is complete, keep it instead
                            # of re-downloading the whole stream.
                            if total_size > 0:
                                try:
                                    disk_size = os.path.getsize(output_path)
                                except OSError:
                                    disk_size = -1
                                if disk_size >= total_size:
                                    await self._broadcast_log(
                                        download_id,
                                        f"Provider ignored Range request; file on disk "
                                        f"({disk_size:,} B) already covers the full "
                                        f"content length ({total_size:,} B). "
                                        f"Keeping existing file.",
                                    )
                                    return disk_size
                            await self._broadcast_log(
                                download_id,
                                "Provider did not honour Range request; re-downloading from start."
                            )
                        return await self._stream_response_to_file(
                            response, output_path, "wb", 0,
                            total_size, download_id, session
                        )

            # Only reached when needs_restart is True: re-request from byte 0.
            async with http_session.get(url) as response:
                if response.status not in (200, 206):
                    raise Exception(f"HTTP {response.status}: {response.reason}")
                restart_ct = response.headers.get("Content-Type", "")
                if restart_ct.lower().startswith("text/"):
                    raise Exception(
                        f"Provider returned an error page (Content-Type: {restart_ct}). "
                        "The catchup window may have expired or be unavailable."
                    )
                total_size = response.content_length or 0
                return await self._stream_response_to_file(
                    response, output_path, "wb", 0,
                    total_size, download_id, session
                )

    async def _post_process(
        self,
        file_path: str,
        download_id: int,
        session: AsyncSession,
        settings: Optional[AppSettings] = None
    ) -> tuple[str, list[str]]:
        """Run post-processing on downloaded file (transcoding, commercial removal)."""
        from services.post_processor import post_processor, OutputFormat, HardwareAccel

        warnings: list[str] = []

        if not settings:
            return file_path, warnings

        current_path = file_path

        if settings.comskip_path:
            post_processor.set_comskip_path(settings.comskip_path)

        # Get hardware acceleration setting
        try:
            hw_accel = HardwareAccel(settings.hw_accel) if settings.hw_accel else HardwareAccel.CPU
        except ValueError:
            hw_accel = HardwareAccel.CPU

        remux_only = getattr(settings, "remux_only", False)

        will_comskip = settings.comskip_enabled and post_processor.comskip_available
        will_transcode = settings.transcode_enabled and post_processor.ffmpeg_available

        if not will_comskip and not will_transcode:
            return current_path, warnings

        # Disk-space preflight: transcoding/commercial removal writes a second
        # copy of the recording next to the original. Refuse to start when free
        # space is already below the configured minimum so ffmpeg cannot die
        # mid-write with ENOSPC and leave a partial output behind.
        min_free_gb = (
            settings.min_free_space_gb if settings.min_free_space_gb is not None else 25
        )
        shortfall = await get_free_space_shortfall(
            self._resolve_download_folder(settings), min_free_gb
        )
        if shortfall:
            free_gb, required_gb = shortfall
            raise Exception(
                f"Not enough disk space to start post-processing. "
                f"{free_gb:.1f} GB free, {required_gb} GB required. "
                f"The raw recording is still in the download folder. "
                f"Free up space and retry."
            )

        async def log_callback(message: str):
            await self._broadcast_log(download_id, message)

        if settings.comskip_enabled and not post_processor.comskip_available:
            await log_callback("Comskip enabled but not available; skipping detection.")
        if not post_processor.ffmpeg_available:
            await log_callback("Post-processing enabled but ffmpeg not available; skipping remux/transcode.")

        download_progress = 100.0
        comskip_progress: Optional[float] = None
        transcode_progress: Optional[float] = None
        comskip_indeterminate = False
        transcode_indeterminate = False

        async def broadcast_processing(progress: float, message: Optional[str], indeterminate: bool = False):
            await self._update_processing(
                session,
                download_id,
                progress,
                message,
                indeterminate=indeterminate,
                download_progress=download_progress,
                comskip_progress=comskip_progress,
                transcode_progress=transcode_progress,
                comskip_indeterminate=comskip_indeterminate,
                transcode_indeterminate=transcode_indeterminate
            )

        await broadcast_processing(0, "Starting post-processing...")
        await log_callback("Post-processing started.")
        last_progress = -1.0
        current_message = None

        async def transcode_progress_callback(p: float):
            nonlocal last_progress
            nonlocal transcode_progress
            if p - last_progress >= 1 or p >= 100:
                last_progress = p
                transcode_progress = p
                await broadcast_processing(p, current_message, indeterminate=transcode_indeterminate)

        # log_callback defined above to also persist logs

        commercials_removed = False

        # Run Comskip if enabled
        if will_comskip:
            try:
                current_message = "Detecting commercials..."
                comskip_progress = 0.0
                comskip_indeterminate = True
                await broadcast_processing(comskip_progress, current_message, indeterminate=True)
                await log_callback("Comskip: detecting commercials.")

                if settings.comskip_path:
                    post_processor.set_comskip_path(settings.comskip_path)

                ffmpeg_path = post_processor.get_ffmpeg_path()
                if ffmpeg_path:
                    await log_callback(f"ffmpeg resolved: {ffmpeg_path}")

                async def comskip_progress_callback(p: float):
                    nonlocal comskip_progress, comskip_indeterminate
                    comskip_progress = p
                    comskip_indeterminate = False
                    await broadcast_processing(comskip_progress, current_message, indeterminate=False)

                edl_path = await post_processor.detect_commercials(
                    current_path,
                    settings.comskip_ini_path,
                    log_callback=log_callback,
                    progress_callback=comskip_progress_callback
                )
                if comskip_progress is None or comskip_progress < 100:
                    comskip_progress = 100.0
                comskip_indeterminate = False
                await broadcast_processing(comskip_progress, current_message, indeterminate=False)

                if edl_path:
                    output_format = OutputFormat(settings.transcode_format or "mkv")
                    accel_name = hw_accel.value if hw_accel != HardwareAccel.CPU else "CPU"
                    if remux_only:
                        current_message = f"Removing commercials + remuxing to {output_format.value}..."
                    else:
                        current_message = (
                            f"Removing commercials + transcoding to {output_format.value} (using {accel_name})..."
                        )
                    transcode_progress = 0.0
                    transcode_indeterminate = False
                    await broadcast_processing(transcode_progress, current_message)
                    await log_callback(
                        f"Comskip: commercials detected. Removing commercials and outputting {output_format.value}."
                    )

                    current_path = await post_processor.remove_commercials(
                        current_path,
                        edl_path,
                        output_format,
                        hw_accel=hw_accel,
                        remove_original=settings.delete_original_after_transcode,
                        progress_callback=transcode_progress_callback,
                        log_callback=log_callback,
                        remux_only=remux_only
                    )
                    commercials_removed = True
                    await log_callback(f"Commercial removal complete: {current_path}")
                else:
                    await log_callback("Comskip: no commercials detected.")
            except Exception as e:
                logger.exception("Comskip error")
                raise RuntimeError(
                    f"ComSkip failed: {e}. The raw recording is still in the download folder and was not deleted."
                ) from e

        # Transcode if enabled (and not already done by commercial removal)
        if will_transcode and not commercials_removed:
            try:
                accel_name = hw_accel.value if hw_accel != HardwareAccel.CPU else "CPU"
                output_format = OutputFormat(settings.transcode_format or "mkv")
                if remux_only:
                    current_message = f"Remuxing to {output_format.value}..."
                else:
                    current_message = f"Transcoding to {output_format.value} (using {accel_name})..."
                transcode_progress = 0.0
                transcode_indeterminate = False
                await broadcast_processing(transcode_progress, current_message)
                await log_callback(
                    f"{'Remuxing' if remux_only else 'Transcoding'} started: {output_format.value}."
                )

                current_path = await post_processor.transcode(
                    current_path,
                    output_format,
                    hw_accel=hw_accel,
                    progress_callback=transcode_progress_callback,
                    log_callback=log_callback,
                    remove_original=settings.delete_original_after_transcode,
                    remux_only=remux_only
                )
                await log_callback(f"{'Remux' if remux_only else 'Transcode'} complete: {current_path}")
            except Exception as e:
                await log_callback(f"Transcode error: {e}")
                warnings.append(f"Transcode failed: {e}")
                logger.exception("Transcode error (continuing anyway)")

        return current_path, warnings

    def _should_log_loop_error(self, key: str, cooldown_seconds: int = 60) -> bool:
        now = datetime.utcnow()
        last_logged = self._loop_error_logged_at.get(key)
        if not last_logged:
            self._loop_error_logged_at[key] = now
            return True
        if (now - last_logged).total_seconds() >= cooldown_seconds:
            self._loop_error_logged_at[key] = now
            return True
        return False

    async def cancel_download(self, download_id: int) -> bool:
        """Cancel a download."""
        self._cancelled.add(download_id)

        cancelled_task = False

        # Cancel active download task if running
        if download_id in self._active_downloads:
            self._active_downloads[download_id].cancel()
            cancelled_task = True

        # Cancel active post-processing task if running
        if download_id in self._active_post:
            self._active_post[download_id].cancel()
            cancelled_task = True

        # Update database status if pending
        async with async_session_maker() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            download = result.scalar_one_or_none()

            if download and download.status in [
                DownloadStatus.PENDING.value,
                DownloadStatus.DOWNLOADING.value,
                DownloadStatus.PROCESSING.value,
            ]:
                download.status = DownloadStatus.CANCELLED.value
                await self._sync_schedule_status(session, download_id, DownloadStatus.CANCELLED.value)
                await session.commit()
                return True

        return cancelled_task

    def _compute_completed_dest(self, path: str, completed_folder: str, download_folder: Optional[str] = None) -> str:
        if download_folder:
            try:
                common = os.path.commonpath([os.path.abspath(path), os.path.abspath(download_folder)])
            except Exception:
                common = None
            if common and os.path.abspath(common) == os.path.abspath(download_folder):
                rel = os.path.relpath(path, download_folder)
                dest = os.path.join(completed_folder, rel)
            else:
                dest = os.path.join(completed_folder, os.path.basename(path))
        else:
            dest = os.path.join(completed_folder, os.path.basename(path))
        return dest

    async def retry_download(self, download_id: int) -> bool:
        """Retry a failed download."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            download = result.scalar_one_or_none()

            if download and download.status in [
                DownloadStatus.FAILED.value,
                DownloadStatus.CANCELLED.value
            ]:
                download.status = DownloadStatus.PENDING.value
                download.progress = 0
                download.downloaded_bytes = 0
                download.error_message = None
                download.completed_at = None
                await self._sync_schedule_status(session, download_id, ScheduledStatus.QUEUED.value)
                await session.commit()

                self._cancelled.discard(download_id)
                await self._queue.put(download_id)
                return True

        return False

    async def get_queue(self) -> list:
        """Get pending and downloading downloads."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(Download).where(
                    Download.status.in_([
                        DownloadStatus.PENDING.value,
                        DownloadStatus.DOWNLOADING.value,
                        DownloadStatus.PROCESSING.value
                    ])
                ).order_by(Download.created_at)
            )
            return [self.merge_progress_snapshot(d.to_dict()) for d in result.scalars().all()]

    async def get_history(self) -> list:
        """Get completed and failed downloads."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(Download).where(
                    Download.status.in_([
                        DownloadStatus.COMPLETED.value,
                        DownloadStatus.FAILED.value,
                        DownloadStatus.CANCELLED.value
                    ])
                ).order_by(Download.created_at.desc())
            )
            return [self.merge_progress_snapshot(d.to_dict()) for d in result.scalars().all()]


# Global instance
download_manager = DownloadManager()
