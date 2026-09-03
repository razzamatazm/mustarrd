import asyncio
import mimetypes
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, conint
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, delete as sql_delete
from typing import Optional

from auth import (
    require_admin_or_download_user,
    require_admin_or_download_user_websocket,
    AuthContext,
)
from config import settings
from database import get_session
from models import AppSettings, Download, DownloadStatus, XtreamAccount, User, ScheduledRecording, ScheduledStatus
from services.disk_space import check_disk_space
from services.download_manager import download_manager
from api.hls_common import hls_asset_response, hls_http_error, hls_playlist_response
from services.hls_streamer import (
    HLS_ASSET_PATTERN,
    HLSError,
    download_session_key,
    hls_streamer,
)
from services.file_namer import file_namer
from services.epg_service import epg_service
from services.download_builder import build_download_from_program
from schedule_timing import get_scheduled_download_delay_minutes


router = APIRouter()
STREAM_CHUNK_SIZE = 1024 * 1024


def _build_content_disposition(disposition: str, filename: str) -> str:
    """
    Build a header-safe Content-Disposition value.
    Uses RFC 5987 filename* for UTF-8 and a conservative ASCII fallback.
    """
    fallback = "".join(ch if 32 <= ord(ch) <= 126 and ch not in {'"', '\\'} else "_" for ch in filename)
    if not fallback:
        fallback = "download.bin"
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _iter_file_bytes(file_path: Path, start: int, end: int):
    with file_path.open("rb") as fh:
        fh.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = fh.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _resolve_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    """
    Parse single-range header forms:
    - bytes=start-end
    - bytes=start-
    - bytes=-suffix_len
    """
    if not range_header.startswith("bytes="):
        raise ValueError("Invalid range unit")
    value = range_header[len("bytes="):].strip()
    if "," in value:
        raise ValueError("Multiple ranges are not supported")
    if "-" not in value:
        raise ValueError("Invalid range format")

    start_raw, end_raw = value.split("-", 1)
    start_raw = start_raw.strip()
    end_raw = end_raw.strip()

    if start_raw == "":
        # suffix range: last N bytes
        suffix_len = int(end_raw)
        if suffix_len <= 0:
            raise ValueError("Invalid suffix length")
        if suffix_len >= file_size:
            return 0, file_size - 1
        return file_size - suffix_len, file_size - 1

    start = int(start_raw)
    if start < 0 or start >= file_size:
        raise ValueError("Range start out of bounds")

    if end_raw == "":
        return start, file_size - 1

    end = int(end_raw)
    if end < start:
        raise ValueError("Invalid range end")
    if end >= file_size:
        end = file_size - 1
    return start, end


class DownloadCreate(BaseModel):
    account_id: int
    channel_id: str
    channel_name: str
    program: dict  # EPG program data
    custom_filename: Optional[str] = None
    pre_padding_minutes: Optional[conint(ge=0, le=120)] = 0
    post_padding_minutes: Optional[conint(ge=0, le=120)] = 0


class FilenamePreview(BaseModel):
    account_id: int
    channel_id: str
    channel_name: str
    program: dict


async def _attach_requested_by(
    session: AsyncSession,
    rows: list[dict],
    auth: AuthContext,
) -> list[dict]:
    if not rows:
        return rows

    if not auth.is_admin:
        if auth.user:
            for row in rows:
                row["requested_by"] = {
                    "user_id": auth.user.id,
                    "username": auth.user.username,
                    "display_name": auth.user.display_name,
                    "provider": auth.provider,
                    "role": auth.user.role,
                }
        return rows

    ids = {row.get("requested_by_user_id") for row in rows if row.get("requested_by_user_id")}
    if not ids:
        if auth.user:
            for row in rows:
                source = (row.get("request_source") or "").lower()
                if source in {"admin", "admin_local"}:
                    row["requested_by"] = {
                        "user_id": auth.user.id,
                        "username": auth.user.username,
                        "display_name": auth.user.display_name,
                        "provider": source or "admin_local",
                        "role": auth.user.role,
                    }
                else:
                    row["requested_by"] = None
        return rows
    result = await session.execute(select(User).where(User.id.in_(ids)))
    users = {u.id: u for u in result.scalars().all()}
    for row in rows:
        rid = row.get("requested_by_user_id")
        if not rid:
            source = (row.get("request_source") or "").lower()
            if auth.user and source in {"admin", "admin_local"}:
                row["requested_by"] = {
                    "user_id": auth.user.id,
                    "username": auth.user.username,
                    "display_name": auth.user.display_name,
                    "provider": source or "admin_local",
                    "role": auth.user.role,
                }
            else:
                row["requested_by"] = None
            continue
        user = users.get(rid)
        row["requested_by"] = (
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "provider": row.get("request_source"),
                "role": user.role,
            }
            if user
            else {
                "user_id": rid,
                "username": None,
                "display_name": "Unknown",
                "provider": row.get("request_source"),
                "role": "unknown",
            }
        )
    return rows


@router.get("/failed-count")
async def get_failed_download_count(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
    since: Optional[float] = Query(default=None, ge=0, le=253402300799000),
):
    """Count permanently failed downloads since a given Unix timestamp in milliseconds."""
    query = select(func.count()).select_from(Download).where(Download.status == DownloadStatus.FAILED.value)
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    query = query.where(Download.completed_at.is_not(None))
    if since is not None:
        try:
            cutoff = datetime.utcfromtimestamp(since / 1000.0)
        except (OSError, OverflowError, ValueError):
            raise HTTPException(status_code=422, detail="since: timestamp out of range")
        query = query.where(Download.completed_at >= cutoff)
    result = await session.execute(query)
    return {"count": result.scalar() or 0}


@router.get("/upcoming")
async def get_upcoming_recordings(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """List scheduled and paused-low-space recordings sorted by air date."""
    query = (
        select(ScheduledRecording)
        .where(
            ScheduledRecording.status.in_([
                ScheduledStatus.SCHEDULED.value,
                ScheduledStatus.PAUSED_LOW_SPACE.value,
            ])
        )
        .order_by(ScheduledRecording.program_start.asc())
    )
    if not auth.is_admin:
        query = query.where(ScheduledRecording.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    recordings = result.scalars().all()
    download_delay_minutes = await get_scheduled_download_delay_minutes(session)
    return [r.to_dict(download_delay_minutes) for r in recordings]


@router.get("")
async def list_downloads(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """List all downloads (queue + history)."""
    query = select(Download).order_by(Download.created_at.desc())
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    downloads = result.scalars().all()
    payload = [download_manager.merge_progress_snapshot(d.to_dict()) for d in downloads]
    return await _attach_requested_by(session, payload, auth)


@router.get("/queue")
async def get_download_queue(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Get pending and active downloads."""
    queue = await download_manager.get_queue()
    filtered = queue if auth.is_admin else [d for d in queue if d.get("requested_by_user_id") == auth.user_id]
    return await _attach_requested_by(session, filtered, auth)


@router.get("/history")
async def get_download_history(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Get completed, failed, and cancelled downloads."""
    history = await download_manager.get_history()
    filtered = history if auth.is_admin else [d for d in history if d.get("requested_by_user_id") == auth.user_id]
    return await _attach_requested_by(session, filtered, auth)


@router.get("/disk-space")
async def get_disk_space(
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Return free disk space on the download folder."""
    result = await session.execute(select(AppSettings))
    app_settings = result.scalar_one_or_none()
    folder = app_settings.download_folder if app_settings else settings.default_download_folder
    min_free_gb = app_settings.min_free_space_gb if app_settings else 25

    folder_exists = await asyncio.to_thread(os.path.exists, folder)
    if not folder_exists:
        return {
            "available": False,
            "disk_free_bytes": None,
            "disk_free_gb": None,
            "min_free_space_gb": min_free_gb,
            "is_low": False,
        }

    usage = await asyncio.to_thread(shutil.disk_usage, folder)
    free_bytes = usage.free
    free_gb = free_bytes / (1024 ** 3)

    return {
        "available": True,
        "disk_free_bytes": free_bytes,
        "disk_free_gb": round(free_gb, 1),
        "min_free_space_gb": min_free_gb,
        "is_low": free_gb < min_free_gb,
    }


@router.post("/preview-filename")
async def preview_filename(
    data: FilenamePreview,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Preview the auto-generated filename for a program."""
    # Get channel info for type detection
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == data.account_id)
    )
    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Build channel dict for file namer
    channel = {
        "name": data.channel_name,
        "stream_id": data.channel_id,
        "category_name": data.program.get("category", ""),
    }

    # Detect program type
    program_type = epg_service.detect_program_type(data.program, channel)

    # Load settings so the preview matches the actual download filename
    settings_result = await session.execute(select(AppSettings))
    app_settings_row = settings_result.scalar_one_or_none()
    filename_settings = app_settings_row.to_dict() if app_settings_row else None

    # Generate filename
    filename = file_namer.generate_filename(data.program, channel, program_type, filename_settings)

    return {
        "filename": filename,
        "detected_type": program_type,
    }


@router.post("")
async def create_download(
    data: DownloadCreate,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Queue a new download."""
    try:
        download = await build_download_from_program(
            session,
            account_id=data.account_id,
            channel_id=data.channel_id,
            channel_name=data.channel_name,
            program=data.program,
            custom_filename=data.custom_filename,
            pre_padding_minutes=data.pre_padding_minutes or 0,
            post_padding_minutes=data.post_padding_minutes or 0,
            requested_by_user_id=auth.user_id,
            request_source=auth.provider or "admin_local",
        )
    except ValueError as exc:
        message = str(exc)
        if "Account not found" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)

    # Reject duplicate: same program already pending, downloading, or processing.
    # Note: this is a read-then-insert check, not atomic. Two simultaneous POSTs
    # (e.g. double-click) can both pass before either commits; sequential duplicates
    # are blocked but a true concurrent race is not fully closed.
    _active = [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value,
    ]
    _dup = await session.execute(
        select(Download.id).where(
            Download.account_id == download.account_id,
            Download.channel_id == download.channel_id,
            Download.start_timestamp == download.start_timestamp,
            Download.stop_timestamp == download.stop_timestamp,
            Download.status.in_(_active),
        ).limit(1)
    )
    if _dup.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="A download for this program is already active.")

    # Scheduled recordings check free space in scheduled_manager; ad-hoc downloads
    # must enforce the same guard here or the threshold is silently bypassed.
    await check_disk_space(session)

    # Queue the download
    try:
        download = await download_manager.queue_download(download)
    except ValueError:
        raise HTTPException(status_code=409, detail="A download for this output path is already active.")

    return (await _attach_requested_by(session, [download.to_dict()], auth))[0]


@router.delete("/finished")
async def clear_finished_downloads(
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete all completed, interrupted and failed download entries."""
    query = sql_delete(Download).where(
        Download.status.in_([
            DownloadStatus.COMPLETED.value,
            DownloadStatus.FAILED.value,
            DownloadStatus.INTERRUPTED.value,
        ])
    )
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    await session.commit()
    return {"deleted": result.rowcount}


@router.get("/{download_id}")
async def get_download(
    download_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Get a specific download."""
    query = select(Download).where(Download.id == download_id)
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    download = result.scalar_one_or_none()

    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    return download.to_dict()


async def _resolve_completed_download_file(
    download_id: int,
    auth: AuthContext,
    session: AsyncSession,
) -> Path:
    """Look up a completed download (scoped to the requesting user) and return
    its on-disk path after validating it lives under an allowed folder."""
    query = select(Download).where(Download.id == download_id)
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    download = result.scalar_one_or_none()

    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    # An interrupted recording is a short but real file sitting in the
    # completed folder, so it is playable and downloadable like a completed one.
    if download.status not in (
        DownloadStatus.COMPLETED.value,
        DownloadStatus.INTERRUPTED.value,
    ):
        raise HTTPException(status_code=409, detail="Download is not completed yet")

    if not download.output_path:
        raise HTTPException(status_code=404, detail="No file path available for this download")

    file_path = Path(download.output_path).expanduser().resolve()
    db_settings_result = await session.execute(select(AppSettings))
    db_settings = db_settings_result.scalar_one_or_none()
    # Union of env defaults and DB-configured folders so recordings made before a
    # folder change remain accessible (de-duplicated after resolve).
    roots_raw = [settings.default_download_folder, settings.default_completed_folder]
    if db_settings:
        if db_settings.download_folder:
            roots_raw.append(db_settings.download_folder)
        if db_settings.completed_folder:
            roots_raw.append(db_settings.completed_folder)
    seen: set = set()
    allowed_roots = []
    for r in roots_raw:
        resolved = Path(r).expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            allowed_roots.append(resolved)
    if not any(file_path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Downloaded file was not found on disk")

    return file_path


@router.get("/{download_id}/file")
async def get_download_file(
    download_id: int,
    request: Request,
    action: str = Query(default="download", pattern="^(download|play)$"),
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve a completed download file for browser download/play actions."""
    file_path = await _resolve_completed_download_file(download_id, auth, session)

    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    disposition = "inline" if action == "play" else "attachment"
    headers = {"Content-Disposition": _build_content_disposition(disposition, file_path.name)}

    if action == "play":
        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise HTTPException(status_code=404, detail="Downloaded file is empty")
        headers["Accept-Ranges"] = "bytes"
        # Tell nginx-style proxies not to buffer full response before streaming.
        headers["X-Accel-Buffering"] = "no"

        range_header = request.headers.get("range") if request else None
        if range_header:
            try:
                start, end = _resolve_byte_range(range_header, file_size)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=416,
                    detail="Invalid Range header",
                    headers={"Content-Range": f"bytes */{file_size}"},
                )

            length = end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            headers["Content-Length"] = str(length)
            return StreamingResponse(
                _iter_file_bytes(file_path, start, end),
                status_code=206,
                media_type=media_type,
                headers=headers,
            )

        headers["Content-Length"] = str(file_size)
        return StreamingResponse(
            _iter_file_bytes(file_path, 0, file_size - 1),
            media_type=media_type,
            headers=headers,
        )

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers=headers,
    )


@router.get("/{download_id}/playback-info")
async def get_download_playback_info(
    download_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Duration metadata for the player's scrub bar (full length up front,
    even while the HLS rendition is still being produced)."""
    file_path = await _resolve_completed_download_file(download_id, auth, session)
    duration = await hls_streamer.probe_duration(file_path)
    return {"duration": duration}


@router.get("/{download_id}/hls/{asset}")
async def get_download_hls_asset(
    download_id: int,
    asset: str,
    start: float = Query(default=0.0, ge=0.0),
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve an on-the-fly HLS rendition of a completed download.

    Requesting playlist.m3u8 starts (or reuses) an FFmpeg repackaging session;
    init.mp4/segments are only served while that session is alive. `start`
    repackages from that offset so the player can scrub ahead of the
    already-produced portion.
    """
    if not HLS_ASSET_PATTERN.match(asset):
        raise HTTPException(status_code=404, detail="Unknown stream asset")

    file_path = await _resolve_completed_download_file(download_id, auth, session)
    key = download_session_key(download_id)

    if asset == "playlist.m3u8":
        try:
            hls_session = await hls_streamer.get_or_create_file(key, file_path, start)
            await hls_streamer.wait_for_playlist(hls_session)
        except HLSError as exc:
            raise hls_http_error(exc)
        return hls_playlist_response(hls_session)

    hls_session = hls_streamer.get_active(key)
    if not hls_session:
        raise HTTPException(status_code=409, detail="No active playback session for this download")
    hls_streamer.touch(hls_session)
    return hls_asset_response(hls_session, asset)


@router.delete("/{download_id}")
async def cancel_download(
    download_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Cancel or remove a download."""
    query = select(Download).where(Download.id == download_id)
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    download = result.scalar_one_or_none()

    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    if download.status in [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value
    ]:
        # Cancel active/pending download
        await download_manager.cancel_download(download_id)
        return {"status": "cancelled"}
    else:
        # Delete from history
        await session.delete(download)
        await session.commit()
        return {"status": "deleted"}


@router.post("/{download_id}/retry")
async def retry_download(
    download_id: int,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    """Retry a failed download."""
    query = select(Download).where(Download.id == download_id)
    if not auth.is_admin:
        query = query.where(Download.requested_by_user_id == auth.user_id)
    result = await session.execute(query)
    download = result.scalar_one_or_none()

    if not download:
        raise HTTPException(status_code=404, detail="Download not found")

    if download.status not in [
        DownloadStatus.FAILED.value,
        DownloadStatus.CANCELLED.value,
        DownloadStatus.INTERRUPTED.value,
    ]:
        raise HTTPException(
            status_code=400,
            detail="Can only retry failed, cancelled or interrupted downloads",
        )

    await check_disk_space(session)

    success = await download_manager.retry_download(download_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to retry download")

    # Refresh download status
    await session.refresh(download)
    return download.to_dict()


@router.websocket("/ws")
async def download_progress_websocket(
    websocket: WebSocket,
    auth: AuthContext = Depends(require_admin_or_download_user_websocket),
):
    """WebSocket for real-time download progress updates."""
    await websocket.accept()
    download_manager.register_websocket(websocket, auth)

    try:
        while True:
            # Keep connection alive, handle any client messages
            data = await websocket.receive_text()
            # Could handle client commands here if needed
    except WebSocketDisconnect:
        pass
    finally:
        download_manager.unregister_websocket(websocket)
