"""Poster art for finished recordings (#443).

When a recording finishes, Mustarrd looks for its poster and does two things
with it:

- remembers which image it is, so History can show a thumbnail; and
- saves a copy into the library next to the recording, named the way Kodi and
  Jellyfin look for it.

It runs off the recording event seam, after the recording is already marked
finished, so nothing here can delay a recording or fail one. No poster is a
normal outcome, not an error.

Where things live:

- ``services/poster_lookup.py`` decides which poster. This module does not
  know what TMDB or TVMaze are.
- ``PosterCache`` is ``logo_cache.LogoCache`` pointed at its own folder. The
  image URL can come out of a third party's JSON, so it gets the same
  treatment a provider logo does: public addresses only, every redirect hop
  re-checked, a size cap, short-lived negative caching.
- ``downloads.poster_url`` (and ``poster_thumb_url``, a small rendition for
  History) is all that is stored about a recording's poster. The bytes are a
  cache and can be deleted at any time; the next request refetches them.

On-demand (VOD) downloads are skipped, like the NFO sidecar: their rows carry
neither guide metadata nor the show's name.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import aiohttp
from sqlalchemy import select

from database import async_session_maker
from models.download import Download, DownloadStatus
from models.settings import AppSettings
from services.credential_crypto import credential_crypto
from services.file_namer import FileNamer
from services.logo_cache import LOGO_FETCH_TIMEOUT, LogoCache
from services.poster_lookup import (
    KIND_TV,
    LOOKUP_USER_AGENT,
    AiohttpJsonClient,
    JsonClient,
    PosterMatch,
    PosterQuery,
    TmdbSource,
    build_sources,
    normalize_title,
    query_from_download,
    resolve_poster,
)
from services.recording_events import RECORDING_COMPLETED, RecordingEvent

logger = logging.getLogger(__name__)

POSTER_MAX_BYTES = 5 * 1024 * 1024
# The whole job for one recording: lookups, the image, the file. Events are
# delivered one at a time, so this is also the longest a poster can hold up
# the next recording's webhook.
ATTACH_TIMEOUT_SECONDS = 45
# Twenty episodes of one show finishing together should cost one lookup.
RESOLVE_CACHE_SECONDS = 24 * 60 * 60
RESOLVE_NEGATIVE_CACHE_SECONDS = 30 * 60
RESOLVE_CACHE_MAX_ENTRIES = 500

LIBRARY_POSTER_SUFFIX = "-poster"
SHOW_FOLDER_POSTER_NAME = "poster"
_POSTER_EXTENSIONS = (".jpg", ".png", ".webp")
_SEASON_FOLDER_RE = re.compile(r"^(season|series|specials|staffel|s)[\s._-]*\d*$", re.IGNORECASE)
_FOLDER_TAG_RE = re.compile(r"\s*[\(\[\{].*$")

_FINISHED_STATUSES = (DownloadStatus.COMPLETED.value, DownloadStatus.INTERRUPTED.value)

OUTCOME_SAVED = "saved"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_DISABLED = "disabled"
OUTCOME_SKIPPED = "skipped"


# --- the image cache --------------------------------------------------------


class PosterCache(LogoCache):
    cache_subdir = "poster_cache"
    max_bytes = POSTER_MAX_BYTES

    def _open_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=LOGO_FETCH_TIMEOUT, headers={"User-Agent": LOOKUP_USER_AGENT}
        )


poster_cache = PosterCache()


def image_extension(content: bytes) -> Optional[str]:
    """The file extension for these image bytes, judged by the bytes themselves.

    ``None`` for anything that is not a JPEG, PNG or WebP: a library file with
    the wrong extension, or an HTML error page saved as ``poster.jpg``, is
    worse than no file.
    """
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None


def content_type_for(content: bytes, declared: str) -> str:
    extension = image_extension(content)
    if extension == ".jpg":
        return "image/jpeg"
    if extension == ".png":
        return "image/png"
    if extension == ".webp":
        return "image/webp"
    return declared


# --- the library file -------------------------------------------------------


def _show_folder_for(video_path: str, show_title: str) -> Optional[str]:
    """The show's own folder, if this recording sits in one.

    ``Bluey/Season 03/Bluey - S03E12.mkv`` and ``Bluey (2018)/Bluey - S03E12.mkv``
    both answer ``Bluey``'s folder. A flat completed folder, or one grouped by
    channel or category, answers ``None``: a ``poster.jpg`` there would put one
    show's face on everything else in the folder.
    """
    wanted = normalize_title(show_title)
    if not wanted:
        return None
    folder = os.path.dirname(os.path.abspath(video_path))
    if _SEASON_FOLDER_RE.match(os.path.basename(folder)):
        folder = os.path.dirname(folder)
    name = _FOLDER_TAG_RE.sub("", os.path.basename(folder))
    return folder if normalize_title(name) == wanted else None


def library_poster_path(
    video_path: str, show_title: str, episodic: bool, extension: str
) -> Tuple[str, bool]:
    """Where the library copy goes: ``(path, shared_with_other_episodes)``.

    Kodi's naming, which Jellyfin and the Plex NFO agents also read:

    - an episode in its show's folder -> ``<show folder>/poster.jpg``
    - anything else                   -> ``<recording name>-poster.jpg``
    """
    if episodic:
        show_folder = _show_folder_for(video_path, show_title)
        if show_folder:
            return os.path.join(show_folder, SHOW_FOLDER_POSTER_NAME + extension), True
    return os.path.splitext(video_path)[0] + LIBRARY_POSTER_SUFFIX + extension, False


def _existing_show_poster(path: str) -> Optional[str]:
    base = os.path.splitext(path)[0]
    for extension in _POSTER_EXTENSIONS:
        candidate = base + extension
        if os.path.exists(candidate):
            return candidate
    return None


def write_library_poster(
    video_path: str,
    content: bytes,
    *,
    show_title: str,
    episodic: bool,
    replace: bool = False,
) -> Optional[str]:
    """Save the poster into the library beside ``video_path``.

    Returns the poster's path, or ``None`` when nothing was written. A show
    folder's ``poster.jpg`` is shared by every episode and may be the user's
    own, so it is left alone unless ``replace`` is set (the refresh action).
    Never raises: like the NFO sidecar, an unwritable library folder must not
    become anybody's error.
    """
    try:
        if not video_path or not os.path.isfile(video_path):
            return None
        extension = image_extension(content)
        if extension is None:
            return None
        target, shared = library_poster_path(video_path, show_title, episodic, extension)
        if shared and not replace:
            existing = _existing_show_poster(target)
            if existing:
                return existing
        tmp_path = target + ".tmp"
        with open(tmp_path, "wb") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
        return target
    except Exception as exc:  # noqa: BLE001 - a poster is a nicety
        logger.warning("Could not save poster art for %s: %s", video_path, exc)
        return None


# --- the TMDB key -----------------------------------------------------------


class TmdbKeyError(ValueError):
    """The text the user pasted cannot be a TMDB key."""


def clean_tmdb_api_key(value: Optional[str]) -> str:
    """Trim a pasted key. Empty means "remove the saved key"."""
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 1000 or re.search(r"[\s\x00-\x1f\x7f]", cleaned):
        raise TmdbKeyError(
            "That does not look like a TMDB key. Paste it without spaces or line breaks."
        )
    return cleaned


def encrypt_tmdb_api_key(value: str) -> Optional[str]:
    return credential_crypto.encrypt(value) if value else None


def tmdb_api_key_from(settings) -> Optional[str]:
    """The saved TMDB key, or ``None`` when there is none or it cannot be read."""
    token = getattr(settings, "tmdb_api_key_encrypted", None)
    if not token:
        return None
    try:
        return credential_crypto.decrypt(token) or None
    except Exception:  # noqa: BLE001 - an unreadable key means "no key", not a crash
        logger.warning("The saved TMDB key could not be read; continuing without it")
        return None


async def check_tmdb_api_key(api_key: str, http: Optional[JsonClient] = None) -> Optional[bool]:
    """True if TMDB accepts the key, False if it refuses it, None if unreachable."""
    try:
        return await TmdbSource(api_key, http or AiohttpJsonClient()).check_key()
    except Exception as exc:  # noqa: BLE001 - the exception text can carry the key
        logger.warning("Could not reach TMDB to check the key (%s)", type(exc).__name__)
        return None


# --- the service ------------------------------------------------------------


@dataclass(frozen=True)
class PosterOutcome:
    status: str
    poster_url: Optional[str] = None
    source: Optional[str] = None
    library_path: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.status == OUTCOME_SAVED


@dataclass(frozen=True)
class _Snapshot:
    """What ``attach`` needs from the database, read once and detached.

    The lookups and the image fetch can take seconds. Holding a database
    session open across them would hold a read lock on SQLite for that long.
    """

    program_title: str
    output_path: str
    poster_url: Optional[str]
    poster_thumb_url: Optional[str]
    poster_source: Optional[str]
    query: Optional[PosterQuery]
    save_to_library: bool
    tmdb_api_key: Optional[str]


class PosterArtService:
    def __init__(
        self,
        cache: Optional[LogoCache] = None,
        http: Optional[JsonClient] = None,
        session_maker=None,
    ) -> None:
        self._cache = cache or poster_cache
        self._http = http or AiohttpJsonClient()
        self._session_maker = session_maker
        self._resolved: Dict[Any, Tuple[float, Optional[PosterMatch]]] = {}

    def _sessions(self):
        return self._session_maker or async_session_maker

    async def attach(self, download_id: int, *, replace: bool = False) -> PosterOutcome:
        """Find and save the poster for one finished recording. Never raises.

        ``replace`` is the refresh action: look again even if a poster is
        already recorded, refetch the image, and overwrite the library copy.
        A refresh that finds nothing keeps the poster the recording had.
        """
        try:
            return await self._attach(download_id, replace=replace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a poster is not the recording's problem
            logger.warning(
                "Poster art step failed for download %s (%s)", download_id, type(exc).__name__
            )
            return PosterOutcome(OUTCOME_NOT_FOUND)

    async def _attach(self, download_id: int, *, replace: bool) -> PosterOutcome:
        snapshot_or_outcome = await self._load(download_id)
        if isinstance(snapshot_or_outcome, PosterOutcome):
            return snapshot_or_outcome
        snapshot = snapshot_or_outcome

        match: Optional[PosterMatch] = None
        if snapshot.poster_url and not replace:
            match = PosterMatch(
                urls=(snapshot.poster_url,),
                source=snapshot.poster_source or "",
                thumbnail_url=snapshot.poster_thumb_url,
            )
        elif snapshot.query is not None:
            match = await self._resolve(snapshot.query, snapshot.tmdb_api_key, fresh=replace)

        if replace and match is not None and match.thumbnail_url:
            self._cache.forget(match.thumbnail_url)
        poster_url, content = await self._fetch_first(match, fresh=replace)
        if poster_url is None or content is None:
            return PosterOutcome(
                OUTCOME_NOT_FOUND,
                poster_url=snapshot.poster_url,
                source=snapshot.poster_source,
            )

        # A thumbnail only stands in for the poster it was offered alongside.
        thumb_url = match.thumbnail_url if match.thumbnail_url != poster_url else None
        if (poster_url, thumb_url, match.source) != (
            snapshot.poster_url,
            snapshot.poster_thumb_url,
            snapshot.poster_source or "",
        ):
            await self._store(download_id, poster_url, thumb_url, match.source)

        library_path = None
        if snapshot.save_to_library:
            episodic = snapshot.query is not None and snapshot.query.kind == KIND_TV
            library_path = await asyncio.to_thread(
                write_library_poster,
                snapshot.output_path,
                content,
                show_title=FileNamer.extract_show_name(snapshot.program_title),
                episodic=episodic,
                replace=replace,
            )

        return PosterOutcome(
            OUTCOME_SAVED, poster_url=poster_url, source=match.source, library_path=library_path
        )

    async def _load(self, download_id: int):
        async with self._sessions()() as session:
            download = (
                await session.execute(select(Download).where(Download.id == download_id))
            ).scalar_one_or_none()
            if download is None or download.is_vod or download.status not in _FINISHED_STATUSES:
                return PosterOutcome(OUTCOME_SKIPPED)
            settings = (await session.execute(select(AppSettings))).scalar_one_or_none()
            if settings is not None and settings.poster_art_enabled is False:
                return PosterOutcome(OUTCOME_DISABLED, poster_url=download.poster_url)
            return _Snapshot(
                program_title=download.program_title or "",
                output_path=download.output_path or "",
                poster_url=download.poster_url,
                poster_thumb_url=download.poster_thumb_url,
                poster_source=download.poster_source,
                query=query_from_download(download),
                save_to_library=(
                    settings is None or settings.poster_art_save_to_library is not False
                ),
                tmdb_api_key=tmdb_api_key_from(settings) if settings is not None else None,
            )

    async def _resolve(
        self, query: PosterQuery, tmdb_api_key: Optional[str], *, fresh: bool
    ) -> Optional[PosterMatch]:
        # Whether there is a key is part of the question: saving one must not
        # be answered from a "nothing found" remembered from before it existed.
        key = (bool(tmdb_api_key), query)
        now = time.monotonic()
        if not fresh:
            cached = self._resolved.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        match = await resolve_poster(query, build_sources(tmdb_api_key, self._http))
        if len(self._resolved) >= RESOLVE_CACHE_MAX_ENTRIES:
            self._resolved.clear()
        ttl = RESOLVE_CACHE_SECONDS if match else RESOLVE_NEGATIVE_CACHE_SECONDS
        self._resolved[key] = (now + ttl, match)
        return match

    async def _fetch_first(
        self, match: Optional[PosterMatch], *, fresh: bool
    ) -> Tuple[Optional[str], Optional[bytes]]:
        for url in (match.urls if match else ()):
            if fresh:
                self._cache.forget(url)
            result = await self._cache.get_logo(url)
            if result is None:
                continue
            content, _content_type = result
            if image_extension(content) is None:
                continue
            return url, content
        return None, None

    async def _store(
        self, download_id: int, poster_url: str, thumb_url: Optional[str], source: str
    ) -> None:
        async with self._sessions()() as session:
            download = (
                await session.execute(select(Download).where(Download.id == download_id))
            ).scalar_one_or_none()
            if download is None:
                return
            download.poster_url = poster_url
            download.poster_thumb_url = thumb_url
            download.poster_source = source or None
            await session.commit()

    async def read(self, *urls: Optional[str]) -> Optional[Tuple[bytes, str]]:
        """The first of ``urls`` that loads, as ``(bytes, content type)``.

        History passes the thumbnail first and the full poster as a fallback.
        Bytes the cache has lost are refetched.
        """
        for url in urls:
            if not url:
                continue
            result = await self._cache.get_logo(url)
            if result is None:
                continue
            content, declared = result
            if image_extension(content) is None:
                continue
            return content, content_type_for(content, declared)
        return None


poster_art = PosterArtService()


# --- the event seam ---------------------------------------------------------


class PosterArtSubscriber:
    """Recording event seam consumer: a finished recording gets its poster."""

    def __init__(self, service: Optional[PosterArtService] = None) -> None:
        self._service = service or poster_art

    async def __call__(self, event: RecordingEvent) -> None:
        if event.name != RECORDING_COMPLETED:
            return
        recording = event.recording or {}
        download_id = recording.get("id")
        if download_id is None or recording.get("is_vod"):
            return
        try:
            await asyncio.wait_for(
                self._service.attach(int(download_id)), timeout=ATTACH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("Poster art for download %s took too long; skipped", download_id)


def register_poster_art_subscriber(bus) -> PosterArtSubscriber:
    """Put the poster consumer on the recording event bus. Called at startup,
    after the webhook and notification consumers so it never keeps them waiting."""
    subscriber = PosterArtSubscriber()
    bus.subscribe(subscriber)
    return subscriber
