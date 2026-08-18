"""Shared HTTP shape for the HLS endpoints.

Downloads serve an HLS rendition of a finished recording; channels serve a
Converted preview of a live provider stream; VOD serves one of a movie or
episode. The sources differ, but what a player sees — the playlist, the
segments, and the failure statuses — must not.
"""

import asyncio

from fastapi import HTTPException
from fastapi.responses import FileResponse

from services.hls_streamer import (
    HLSError,
    HLSLimitError,
    HLSSession,
    HLSUnavailableError,
)
from services.preview_budget import PreviewLimitError, preview_budget

# 429: the viewer can retry after closing another player.
# 503: the server is missing FFmpeg, which no retry fixes.
# 502: this particular stream could not be prepared.
_HLS_ERROR_STATUS = {
    HLSLimitError: 429,
    HLSUnavailableError: 503,
}


# Strong refs to in-flight cleanup tasks: the event loop only holds weak
# references to tasks, so an unanchored one could be collected before it runs.
_pending_cleanups: set = set()


def detach_cleanup(coro) -> None:
    """Run a cleanup coroutine without awaiting it.

    Preview teardown routinely runs with a CancelledError pending — a client
    that disconnects cancels the streaming task — and the first await in that
    state re-raises, skipping everything after it. Detaching the awaitable part
    lets the caller's cleanup stay await-free and therefore uninterruptible.
    """
    task = asyncio.ensure_future(coro)
    _pending_cleanups.add(task)
    task.add_done_callback(_pending_cleanups.discard)


async def close_provider_connection(provider_response, http_session) -> None:
    """Close a provider response and its session, tolerating partial setup."""
    try:
        if provider_response is not None:
            provider_response.close()
    finally:
        if http_session is not None and not http_session.closed:
            await http_session.close()


def acquire_preview_slot() -> None:
    """Take one of the shared preview slots, or refuse the request with 429."""
    try:
        preview_budget.acquire()
    except PreviewLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


def release_preview_slot() -> None:
    preview_budget.release()


def hls_http_error(exc: HLSError) -> HTTPException:
    return HTTPException(status_code=_HLS_ERROR_STATUS.get(type(exc), 502), detail=str(exc))


def hls_playlist_response(session: HLSSession) -> FileResponse:
    return FileResponse(
        path=str(session.playlist_path),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


def hls_asset_response(session: HLSSession, asset: str) -> FileResponse:
    """Serve one init/segment file. `asset` must already be pattern-validated,
    which is what keeps it from escaping the session directory."""
    asset_path = session.directory / asset
    if not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Stream segment not found")
    return FileResponse(
        path=str(asset_path),
        media_type="video/mp4" if asset.endswith(".mp4") else "video/iso.segment",
        headers={"Cache-Control": "no-store"},
    )
