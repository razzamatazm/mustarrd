import time
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Annotated, Dict, Optional
import aiohttp
import logging
import os

from auth import require_admin_or_download_user, AuthContext
from api.hls_common import (
    acquire_preview_slot,
    close_provider_connection,
    detach_cleanup,
    hls_asset_response,
    hls_http_error,
    hls_playlist_response,
    release_preview_slot,
)
from database import get_session
from models import XtreamAccount
from services.account_credentials import resolve_account_password_with_migration
from services.disk_space import check_disk_space
from services.hls_streamer import (
    HLS_ASSET_PATTERN,
    HLSError,
    hls_streamer,
    vod_preview_session_key,
)
from services.preview_budget import preview_budget
from services.vod_preview_source import loopback_source_url, vod_preview_source_relay
from services.xtream_client import XtreamClient
from services.vod_service import build_movie_download, build_episode_download
from services.download_manager import download_manager


router = APIRouter()
logger = logging.getLogger(__name__)

# A VOD preview can be scrubbed, so the 300s cap that suits a live preview
# would mean never seeing past the first five minutes of a film. The ceiling
# here is wall-clock on the whole session instead: enough to sample four points
# across a two-hour film, not enough to be a player.
VOD_PREVIEW_MAX_SECONDS = 15 * 60
# How long after its ceiling a preview stays refused. Long enough that the cap
# is a real stop rather than something a player's own retry sails through,
# short enough that a viewer who genuinely wants another look is not locked out.
PREVIEW_CEILING_LOCKOUT_SECONDS = 60

# Provider ids are numeric on every provider seen so far; the pattern keeps a
# hostile id out of the URL FFmpeg is pointed at and out of the session key.
ITEM_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
CONTAINER_EXTENSION_PATTERN = r"^[A-Za-z0-9]{1,8}$"

SOURCE_RELAY_CHUNK_SIZE = 256 * 1024
# No total timeout: the relay is read for as long as the preview runs. FFmpeg
# reopens the connection on every seek, so a stalled provider surfaces as a
# read timeout rather than a permanently wedged session.
SOURCE_RELAY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# Headers a request only carries if something forwarded it. FFmpeg sends none
# of them, so their presence means the caller is not the child process this
# endpoint exists for.
PROXIED_REQUEST_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")

# When each preview's ceiling falls due, keyed by session. The ceiling belongs
# to the *preview*, not to the FFmpeg process behind it: scrubbing rebuilds that
# process, and a per-process ceiling would restart the clock every time the
# viewer moved the scrub bar — which is exactly how you would watch a whole film
# through something that is not supposed to be a player.
_preview_ceilings: Dict[str, float] = {}


class MovieDownloadRequest(BaseModel):
    account_id: int
    vod_id: str
    name: str
    container_extension: Optional[str] = None
    direct_source: Optional[str] = None
    release_date: Optional[str] = None


class EpisodeItem(BaseModel):
    id: str
    season: int
    episode_num: int
    title: Optional[str] = None
    container_extension: Optional[str] = None
    direct_source: Optional[str] = None
    duration_minutes: Optional[int] = None


class SeriesDownloadRequest(BaseModel):
    account_id: int
    series_id: str
    series_name: str
    episodes: Annotated[list[EpisodeItem], Field(max_length=200)]


async def _get_client(session: AsyncSession, account: XtreamAccount) -> XtreamClient:
    password = await resolve_account_password_with_migration(session, account)
    return XtreamClient(account.server_url, account.username, password)


async def _get_account(session: AsyncSession, account_id: int) -> XtreamAccount:
    result = await session.execute(
        select(XtreamAccount).where(XtreamAccount.id == account_id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/movies/categories")
async def get_movie_categories(
    account_id: int,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_vod_categories()
    except Exception:
        logger.exception("Failed to load VOD categories account_id=%s", account_id)
        raise HTTPException(status_code=400, detail="Failed to load VOD categories from provider")
    finally:
        await client.close()


@router.get("/movies")
async def get_movies(
    account_id: int,
    category_id: Optional[str] = None,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_vod_streams(category_id)
    except Exception:
        logger.exception(
            "Failed to load VOD streams account_id=%s category_id=%s",
            account_id,
            category_id,
        )
        raise HTTPException(status_code=400, detail="Failed to load VOD content from provider")
    finally:
        await client.close()


@router.get("/movies/{vod_id}")
async def get_movie_info(
    vod_id: str,
    account_id: int,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_vod_info(vod_id)
    except Exception:
        logger.exception("Failed to load VOD info account_id=%s vod_id=%s", account_id, vod_id)
        raise HTTPException(status_code=400, detail="Failed to load VOD details from provider")
    finally:
        await client.close()


@router.post("/movies/download")
async def download_movie(
    data: MovieDownloadRequest,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    try:
        download = await build_movie_download(
            session,
            account_id=data.account_id,
            vod_id=data.vod_id,
            title=data.name,
            container_extension=data.container_extension,
            direct_source=data.direct_source,
            release_date=data.release_date,
            requested_by_user_id=auth.user_id,
            request_source=auth.provider or "admin_local",
        )
    except ValueError as exc:
        logger.exception(
            "Failed to queue movie download account_id=%s vod_id=%s",
            data.account_id,
            data.vod_id,
        )
        message = str(exc)
        if "Account not found" in message:
            raise HTTPException(status_code=404, detail="Account not found")
        raise HTTPException(status_code=400, detail="Unable to queue movie download")

    await check_disk_space(session)
    try:
        download = await download_manager.queue_download(download)
    except ValueError:
        raise HTTPException(status_code=409, detail="A download for this file is already active.")
    return download.to_dict()


@router.get("/series/categories")
async def get_series_categories(
    account_id: int,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_series_categories()
    except Exception:
        logger.exception("Failed to load series categories account_id=%s", account_id)
        raise HTTPException(status_code=400, detail="Failed to load series categories from provider")
    finally:
        await client.close()


@router.get("/series")
async def get_series(
    account_id: int,
    category_id: Optional[str] = None,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_series(category_id)
    except Exception:
        logger.exception(
            "Failed to load series account_id=%s category_id=%s",
            account_id,
            category_id,
        )
        raise HTTPException(status_code=400, detail="Failed to load series from provider")
    finally:
        await client.close()


@router.get("/series/{series_id}")
async def get_series_info(
    series_id: str,
    account_id: int,
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        return await client.get_series_info(series_id)
    except Exception:
        logger.exception("Failed to load series info account_id=%s series_id=%s", account_id, series_id)
        raise HTTPException(status_code=400, detail="Failed to load series details from provider")
    finally:
        await client.close()


@router.post("/series/download")
async def download_series(
    data: SeriesDownloadRequest,
    auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session)
):
    # Build every episode first; nothing is persisted until the whole batch
    # validates, so a mid-batch failure cannot leave a partial episode set.
    downloads = []
    for episode in data.episodes:
        try:
            download = await build_episode_download(
                session,
                account_id=data.account_id,
                series_id=data.series_id,
                show_name=data.series_name,
                episode_id=episode.id,
                season=episode.season,
                episode_num=episode.episode_num,
                episode_title=episode.title,
                container_extension=episode.container_extension,
                direct_source=episode.direct_source,
                duration_minutes=episode.duration_minutes,
                requested_by_user_id=auth.user_id,
                request_source=auth.provider or "admin_local",
            )
        except ValueError as exc:
            logger.exception(
                "Failed to queue series episode download account_id=%s series_id=%s episode_id=%s",
                data.account_id,
                data.series_id,
                episode.id,
            )
            message = str(exc)
            if "Account not found" in message:
                raise HTTPException(status_code=404, detail="Account not found")
            raise HTTPException(status_code=400, detail="Unable to queue series download")
        downloads.append(download)

    if not downloads:
        return {"count": 0, "downloads": []}

    await check_disk_space(session)

    # One IN query for the whole batch instead of one duplicate check per episode.
    paths = [d.output_path for d in downloads]
    conflicts = set(await download_manager.find_active_output_conflicts(session, paths))
    conflicts.update(path for path, n in Counter(paths).items() if n > 1)
    if conflicts:
        names = ", ".join(sorted(os.path.basename(str(path)) for path in conflicts))
        raise HTTPException(
            status_code=409,
            detail=f"A download is already active for: {names}",
        )

    # All-or-nothing: a single commit covers the whole batch, and the episodes
    # are only enqueued after that commit succeeds.
    for download in downloads:
        session.add(download)
    await session.commit()

    queued = []
    for download in downloads:
        await download_manager.enqueue_persisted(download)
        queued.append(download.to_dict())

    return {
        "count": len(queued),
        "downloads": queued,
    }


# --------------------------------------------------------------- VOD preview
#
# Previewing a movie or an episode is the same problem as previewing a live
# channel with one addition: it must seek. Nobody decides whether a two-hour
# film is the right one from its first thirty seconds, and providers ship VOD
# as MKV (94% of movies, 99.8% of episodes) or as MP4 with AC-3 audio — none of
# which a browser can decode. So there is no Direct path here at all: every VOD
# preview is FFmpeg-converted, and FFmpeg is given a *seekable* source, namely
# the loopback relay below. See services/vod_preview_source.py for why the
# provider URL cannot simply be handed over instead.


def _preview_seconds_remaining(key: str) -> float:
    """Seconds left on this preview's ceiling, starting the clock on first use."""
    now = time.monotonic()
    for stale_key, deadline in list(_preview_ceilings.items()):
        if now > deadline + PREVIEW_CEILING_LOCKOUT_SECONDS:
            del _preview_ceilings[stale_key]
    deadline = _preview_ceilings.get(key)
    if deadline is None:
        deadline = now + VOD_PREVIEW_MAX_SECONDS
        _preview_ceilings[key] = deadline
    return deadline - now


def _is_loopback_caller(request: Request) -> bool:
    """Whether this looks like our own FFmpeg rather than someone else.

    The peer address is the primary check, but it is worth less than it looks:
    a reverse proxy sharing this process's network namespace makes *every*
    request arrive from 127.0.0.1, and the check stops distinguishing anything.
    So a request carrying proxy headers is refused as well — FFmpeg does not
    send them, and a proxy forwarding a request almost always adds them. That
    is defence in depth, not a guarantee; see docs/adr for the stronger option.
    """
    client = request.client
    if client is None or client.host not in LOOPBACK_CLIENT_HOSTS:
        return False
    return not any(header in request.headers for header in PROXIED_REQUEST_HEADERS)


def _loopback_port(request: Request) -> int:
    """The port FFmpeg should dial back on.

    Read from the socket the request arrived on rather than from a header or a
    setting: it is the address this process is genuinely bound to, which is
    still true behind a reverse proxy that fronts us on a different port.
    """
    server = request.scope.get("server")
    if not server or not server[1]:
        raise HTTPException(
            status_code=500,
            detail="Preview is unavailable: the server port could not be determined",
        )
    return int(server[1])


async def _resolve_vod_preview_url(
    session: AsyncSession,
    account_id: int,
    kind: str,
    item_id: str,
    container_extension: Optional[str],
) -> str:
    """Build the provider URL for a movie or episode. Carries the account
    username and password, so it must never reach a response, an error
    message, or a process argument list."""
    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    try:
        if kind == "movie":
            return client.build_vod_url(item_id, container_extension)
        return client.build_series_url(item_id, container_extension)
    finally:
        await client.close()


def _preview_fingerprint(
    kind: str, item_id: str, container_extension: Optional[str], start: float
) -> str:
    """Identify what a session is rendering. `start` is part of it, so scrubbing
    to a new offset rebuilds the session rather than replaying from zero.
    Derived only from request parameters, never from the credentialed URL."""
    return f"{kind}:{item_id}:{container_extension or ''}:{start:.3f}"


def _parse_duration(raw) -> Optional[float]:
    """Seconds from either a numeric duration or a provider's "H:MM:SS"."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text) if float(text) > 0 else None
    except ValueError:
        pass
    parts = text.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _duration_from_info(info) -> Optional[float]:
    if not isinstance(info, dict):
        return None
    for key in ("duration_secs", "duration_sec", "duration"):
        duration = _parse_duration(info.get(key))
        if duration is not None:
            return duration
    return None


def _episode_duration(series_info: dict, episode_id: str) -> Optional[float]:
    seasons = (series_info or {}).get("episodes") or {}
    if isinstance(seasons, dict):
        seasons = seasons.values()
    for episodes in seasons:
        for episode in episodes or []:
            if str(episode.get("id")) != str(episode_id):
                continue
            return _duration_from_info(episode.get("info")) or _duration_from_info(episode)
    return None


@router.get("/preview/source/{token}")
async def vod_preview_source(token: str, request: Request):
    """Serve the provider bytes for a preview to FFmpeg, ranges included.

    Deliberately not browser-facing and deliberately not session-authenticated:
    FFmpeg has no cookie. What guards it is that the caller must be on loopback
    — this process talking to a child of itself — and must present a token this
    process minted moments earlier for one specific title.

    Ranges pass through in both directions, which is the whole point: it is
    what makes the source seekable, and therefore the preview scrubbable. The
    provider's 302 to an edge node is followed here and never handed back to
    the caller — that redirect target is a bearer token for the account's
    stream even though it carries no password.
    """
    if not _is_loopback_caller(request):
        raise HTTPException(status_code=403, detail="Not available")

    url = vod_preview_source_relay.resolve(token)
    if url is None:
        raise HTTPException(status_code=404, detail="Not available")

    request_headers = {}
    range_header = request.headers.get("range")
    if range_header:
        request_headers["Range"] = range_header

    http_session = aiohttp.ClientSession(timeout=SOURCE_RELAY_TIMEOUT)
    try:
        provider = await http_session.get(url, headers=request_headers)
    except Exception:
        await http_session.close()
        logger.exception("VOD preview source relay could not reach the provider")
        raise HTTPException(status_code=502, detail="Could not reach the provider")

    if provider.status == 416:
        headers = {"Content-Range": provider.headers.get("Content-Range", "bytes */*")}
        detach_cleanup(close_provider_connection(provider, http_session))
        return Response(status_code=416, headers=headers)

    if provider.status not in (200, 206):
        status = provider.status
        detach_cleanup(close_provider_connection(provider, http_session))
        # Never echo the URL: it carries credentials.
        raise HTTPException(
            status_code=502, detail=f"Provider refused the source (HTTP {status})"
        )

    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for name in ("Content-Length", "Content-Range"):
        value = provider.headers.get(name)
        if value:
            headers[name] = value

    async def relay():
        try:
            async for chunk in provider.content.iter_chunked(SOURCE_RELAY_CHUNK_SIZE):
                yield chunk
        finally:
            detach_cleanup(close_provider_connection(provider, http_session))

    return StreamingResponse(
        relay(),
        status_code=provider.status,
        media_type=provider.headers.get("Content-Type") or "application/octet-stream",
        headers=headers,
    )


@router.get("/preview/{account_id}/{kind}/{item_id}/duration")
async def vod_preview_duration(
    account_id: int,
    request: Request,
    kind: str = Path(..., pattern="^(movie|episode)$"),
    item_id: str = Path(..., pattern=ITEM_ID_PATTERN),
    series_id: Optional[str] = Query(None, pattern=ITEM_ID_PATTERN),
    container_extension: Optional[str] = Query(None, pattern=CONTAINER_EXTENSION_PATTERN),
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """The title's full length, so the scrub bar is full-length from the first
    frame instead of growing as FFmpeg produces segments.

    The provider's own metadata answers this for nearly everything; a probe is
    the fallback, and it goes through the same loopback relay so it too never
    sees the credentialed URL.
    """
    if kind == "episode" and not series_id:
        raise HTTPException(status_code=400, detail="series_id is required for an episode")

    account = await _get_account(session, account_id)
    client = await _get_client(session, account)
    duration = None
    try:
        if kind == "movie":
            info = await client.get_vod_info(item_id)
            duration = _duration_from_info((info or {}).get("info"))
        else:
            info = await client.get_series_info(series_id)
            duration = _episode_duration(info or {}, item_id)
    except Exception:
        # Metadata is a convenience here, not the answer: fall through to the
        # probe rather than failing the preview over it.
        logger.warning(
            "VOD preview duration metadata unavailable account_id=%s kind=%s",
            account_id, kind, exc_info=True,
        )
    finally:
        await client.close()

    if duration is not None:
        return {"duration": duration}

    stream_url = await _resolve_vod_preview_url(
        session, account_id, kind, item_id, container_extension
    )
    # The probe reaches the provider, so it spends a slot like any other
    # preview connection would — the budget exists to bound connections, not
    # players.
    acquire_preview_slot()
    token = vod_preview_source_relay.mint(stream_url)
    try:
        duration = await hls_streamer.probe_duration(
            loopback_source_url(_loopback_port(request), token)
        )
    finally:
        vod_preview_source_relay.revoke(token)
        release_preview_slot()
    return {"duration": duration}


@router.get("/preview/{account_id}/{kind}/{item_id}/hls/{asset}")
async def vod_preview_hls_asset(
    account_id: int,
    request: Request,
    kind: str = Path(..., pattern="^(movie|episode)$"),
    item_id: str = Path(..., pattern=ITEM_ID_PATTERN),
    asset: str = Path(...),
    container_extension: Optional[str] = Query(None, pattern=CONTAINER_EXTENSION_PATTERN),
    start: float = Query(0.0, ge=0.0),
    _auth: AuthContext = Depends(require_admin_or_download_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve a converted preview of a movie or episode.

    Requesting playlist.m3u8 starts (or reuses) the FFmpeg session; `start`
    restarts it at that offset, which is how scrubbing past what FFmpeg has
    already produced works. Segments carry no query string, so they resolve by
    path alone.
    """
    if not HLS_ASSET_PATTERN.match(asset):
        raise HTTPException(status_code=404, detail="Unknown stream asset")

    key = vod_preview_session_key(account_id, kind, item_id)

    if asset != "playlist.m3u8":
        hls_session = hls_streamer.get_active(key)
        if not hls_session:
            raise HTTPException(status_code=409, detail="No active preview session for this title")
        hls_streamer.touch(hls_session)
        return hls_asset_response(hls_session, asset)

    fingerprint = _preview_fingerprint(kind, item_id, container_extension, start)
    existing = hls_streamer.get_active(key)
    if existing is not None and existing.fingerprint == fingerprint:
        hls_streamer.touch(existing)
        return hls_playlist_response(existing)

    stream_url = await _resolve_vod_preview_url(
        session, account_id, kind, item_id, container_extension
    )
    port = _loopback_port(request)

    remaining = _preview_seconds_remaining(key)
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail="This preview has reached its time limit. Previews stop after 15 minutes.",
        )

    acquire_preview_slot()
    token = vod_preview_source_relay.mint(stream_url)
    released = False

    def release():
        # One-shot: the streamer calls this when the session dies, and this
        # handler calls it when the session never came to life. The grant dies
        # with the session it was minted for.
        nonlocal released
        if released:
            return
        released = True
        vod_preview_source_relay.revoke(token)
        release_preview_slot()
        logger.info(
            "VOD preview slot released account_id=%s kind=%s item_id=%s active=%s",
            account_id, kind, item_id, preview_budget.active,
        )

    hls_session = None

    async def abandon():
        # Release first: under cancellation the await below never returns.
        release()
        if hls_session is not None:
            await hls_streamer.close_session(hls_session)

    try:
        hls_session = await hls_streamer.get_or_create_url(
            key,
            loopback_source_url(port, token),
            fingerprint,
            remaining,
            start_offset=start,
            on_close=release,
        )
        await hls_streamer.wait_for_playlist(hls_session)
    except HLSError as exc:
        await abandon()
        raise hls_http_error(exc)
    except BaseException:
        await abandon()
        raise

    logger.info(
        "VOD preview started account_id=%s kind=%s item_id=%s start=%.1f active=%s",
        account_id, kind, item_id, start, preview_budget.active,
    )
    return hls_playlist_response(hls_session)
