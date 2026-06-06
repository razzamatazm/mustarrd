import asyncio
import aiohttp
import aiofiles
import logging
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, Dict, Set, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from models import Download, DownloadStatus, AppSettings, XtreamAccount, PlexServer
from config import settings as app_settings, is_docker_env
from database import async_session_maker
from services.log_stream import backend_log_stream
from services.credential_crypto import credential_crypto
from services.plex_service import plex_service

logger = logging.getLogger(__name__)


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

    async def queue_download(self, download: Download) -> Download:
        """Add a download to the queue."""
        async with async_session_maker() as session:
            session.add(download)
            await session.commit()
            await session.refresh(download)
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
        return True

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
                        is_complete = (
                            input_file.is_file()
                            and download.file_size
                            and download.downloaded_bytes >= int(download.file_size)
                        )
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
                            completed_path = self._move_to_completed(str(input_file), completed_folder, download_folder)
                            download.output_path = completed_path
                            download.status = DownloadStatus.COMPLETED.value
                            download.progress = 100.0
                            if not download.completed_at:
                                download.completed_at = datetime.utcnow()
                            download.error_message = None
                            await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    download.status = DownloadStatus.PENDING.value
                    download.progress = 0.0
                    download.downloaded_bytes = 0
                    download.file_size = 0
                    download.error_message = None
                    recovered_download_ids.append(download.id)
                    await self._broadcast_log(download.id, "Recovered after restart: requeued for download.")
                    continue

                if download.status == DownloadStatus.PROCESSING.value:
                    if input_file.is_file() and self._path_is_under(str(input_file), completed_folder):
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        if not download.completed_at:
                            download.completed_at = datetime.utcnow()
                        download.error_message = None
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
                            completed_path = self._move_to_completed(str(input_file), completed_folder, download_folder)
                            download.output_path = completed_path
                            download.status = DownloadStatus.COMPLETED.value
                            download.progress = 100.0
                            if not download.completed_at:
                                download.completed_at = datetime.utcnow()
                            download.error_message = None
                            await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    # If we lost the input file but still have an output, finalize it.
                    processed_found = None
                    for candidate in self._processed_output_candidates(input_path, settings):
                        if Path(candidate).is_file():
                            processed_found = candidate
                            break
                    if processed_found:
                        completed_path = self._move_to_completed(processed_found, completed_folder, download_folder)
                        download.output_path = completed_path
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        if not download.completed_at:
                            download.completed_at = datetime.utcnow()
                        download.error_message = None
                        await self._broadcast_log(download.id, "Recovered after restart: finalized completed output.")
                        continue

                    download.status = DownloadStatus.FAILED.value
                    download.error_message = "Recovery failed: missing input file for post-processing."
                    await self._broadcast_log(download.id, download.error_message, level="error")

            await session.commit()

        for download_id in recovered_download_ids:
            await self._queue.put(download_id)
        for download_id in recovered_post_ids:
            await self._post_queue.put(download_id)
        return len(recovered_download_ids) + len(recovered_post_ids)

    async def _execute_download(self, download_id: int):
        """Execute a single download."""
        async with async_session_maker() as session:
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
                    session
                )
                if downloaded_bytes == 0:
                    raise Exception(
                        "Provider returned an empty response. "
                        "The catchup window for this program may have expired."
                    )
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
                completed_path = self._move_to_completed(download.output_path, completed_folder, download_folder)
                download.output_path = completed_path

                download.status = DownloadStatus.COMPLETED.value
                download.progress = 100.0
                download.completed_at = datetime.utcnow()
                download.error_message = None
                await session.commit()

                await self._broadcast_progress(
                    download_id, 100, DownloadStatus.COMPLETED.value
                )
                await self._broadcast_log(
                    download_id,
                    f"Download completed: {os.path.basename(completed_path)}"
                )
                await self._trigger_plex_refresh(completed_path)

            except asyncio.CancelledError:
                # Download was cancelled
                download.status = DownloadStatus.CANCELLED.value
                await session.commit()
                if download.output_path and os.path.exists(download.output_path):
                    try:
                        os.unlink(download.output_path)
                    except OSError:
                        pass
                await self._broadcast_progress(
                    download_id, download.progress, DownloadStatus.CANCELLED.value
                )
                await self._broadcast_log(download_id, "Download cancelled.", level="warning")

            except Exception as e:
                # Download failed
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()
                if download:
                    download.status = DownloadStatus.FAILED.value
                    download.error_message = str(e)
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
        async with async_session_maker() as session:
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
                original_file = Path(original_path)
                if not original_file.is_file():
                    processed_found = None
                    for candidate in self._processed_output_candidates(original_path, settings):
                        if Path(candidate).is_file():
                            processed_found = candidate
                            break
                    if processed_found:
                        completed_path = self._move_to_completed(processed_found, completed_folder, download_folder)
                        download.output_path = completed_path
                        download.status = DownloadStatus.COMPLETED.value
                        download.progress = 100.0
                        download.completed_at = datetime.utcnow()
                        download.error_message = None
                        await session.commit()
                        await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)
                        await self._broadcast_log(download_id, "Post-processing recovery: finalized completed output.")
                        await self._trigger_plex_refresh(completed_path)
                        return

                    raise Exception("Missing input file for post-processing.")

                if not self._needs_post_processing(download, settings):
                    completed_path = self._move_to_completed(original_path, completed_folder, download_folder)
                    download.output_path = completed_path
                    download.status = DownloadStatus.COMPLETED.value
                    download.progress = 100.0
                    download.completed_at = datetime.utcnow()
                    download.error_message = None
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
                completed_path = self._move_to_completed(final_path, completed_folder, download_folder)
                download.output_path = completed_path
                if warnings:
                    download.error_message = f"Completed with warnings: {'; '.join(warnings)}"
                    await self._broadcast_log(
                        download_id,
                        f"Completed with warnings: {'; '.join(warnings)}",
                        level="warning"
                    )
                else:
                    download.error_message = None

                self._cleanup_working_files(
                    original_path,
                    completed_path,
                    keep_logs=bool(warnings)
                )

                download.status = DownloadStatus.COMPLETED.value
                download.progress = 100.0
                download.completed_at = datetime.utcnow()
                await session.commit()

                await self._broadcast_progress(download_id, 100, DownloadStatus.COMPLETED.value)
                await self._broadcast_log(
                    download_id,
                    f"Post-processing completed: {os.path.basename(completed_path)}"
                )
                await self._trigger_plex_refresh(completed_path)

            except asyncio.CancelledError:
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
                    await session.commit()
                    await self._broadcast_progress(
                        download_id, download.progress, DownloadStatus.CANCELLED.value
                    )
                    await self._broadcast_log(download_id, "Post-processing cancelled.", level="warning")

            except Exception as e:
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                download = result.scalar_one_or_none()
                if download:
                    download.status = DownloadStatus.FAILED.value
                    download.error_message = str(e)
                    await session.commit()

                await self._broadcast_progress(
                    download_id,
                    download.progress if download else 0,
                    DownloadStatus.FAILED.value,
                    error=str(e)
                )
                await self._broadcast_log(download_id, f"Post-processing failed: {e}", level="error")

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
        shutil.move(path, dest)
        return dest

    def _cleanup_working_files(self, original_path: str, completed_path: str, keep_logs: bool) -> None:
        try:
            original_file = Path(original_path)
            base_dir = original_file.parent
            stem = original_file.stem
            completed_real = os.path.realpath(os.path.abspath(completed_path))

            if original_path != completed_path and original_file.exists():
                original_file.unlink()

            patterns = [
                f"{stem}_seg*.ts",
                f"{stem}.concat.txt",
                f"{stem}.edl",
                f"{stem}.txt",
                f"{stem}.logo",
                f"{stem}.csv",
                f"{stem}.vdr",
                f"{stem}.xml",
                f"{stem}.srt",
                f"{stem}.ass",
                f"{stem}.vtt",
            ]
            if not keep_logs:
                patterns.extend([
                    f"{stem}.log",
                    f"{stem}.*.ffmpeg.log",
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

    async def _download_file(
        self,
        url: str,
        output_path: str,
        download_id: int,
        session: AsyncSession
    ):
        """Stream download a file with progress tracking."""
        timeout = aiohttp.ClientTimeout(total=None, sock_read=60)

        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with http_session.get(url) as response:
                if response.status not in (200, 206):
                    raise Exception(f"HTTP {response.status}: {response.reason}")

                total_size = response.content_length or 0
                if response.status == 206:
                    content_range = response.headers.get("Content-Range")
                    if content_range and "/" in content_range:
                        total_part = content_range.split("/")[-1].strip()
                        if total_part.isdigit():
                            total_size = int(total_part)
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
                downloaded = 0
                last_progress_update = 0

                async with aiofiles.open(output_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                        # Check for cancellation
                        if download_id in self._cancelled:
                            raise asyncio.CancelledError()

                        await f.write(chunk)
                        downloaded += len(chunk)

                        # Update progress every 1%
                        if total_size > 0:
                            progress = (downloaded / total_size) * 100
                        else:
                            progress = 0

                        if progress - last_progress_update >= 1 or downloaded == total_size:
                            last_progress_update = progress

                            # Update database
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

                            # Broadcast progress
                            await self._broadcast_progress(
                                download_id,
                                progress,
                                DownloadStatus.DOWNLOADING.value,
                                downloaded_bytes=downloaded,
                                file_size=total_size,
                                download_progress=progress
                            )
                await self._broadcast_log(
                    download_id,
                    f"Download bytes written: {downloaded:,}."
                )
                return downloaded

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
        will_transcode = post_processor.ffmpeg_available

        if not will_comskip and not will_transcode:
            return current_path, warnings

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
                await log_callback(f"Comskip error: {e}")
                warnings.append(f"Comskip failed: {e}")
                logger.exception("Comskip error (continuing anyway)")

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
