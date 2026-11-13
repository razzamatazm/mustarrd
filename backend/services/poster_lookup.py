"""Finding a poster for a recording: who to ask, and what to ask them (#443).

Two sources, asked in order, first answer wins:

1. **TMDB**, only when the user has saved an API key.
2. **TVMaze**, which needs no key but only knows TV shows.

Both are asked by ID first. The guide sometimes publishes a TMDB, TVDB or IMDb
ID for a program (``guide_metadata``), and an ID cannot land on the wrong
show. Most guides publish none, so the fallback is a search by title, and a
search result is only accepted when its name is the recording's name once
case, accents and punctuation are set aside. A recording with no poster is
fine; a recording with another show's poster is not.

This module is free of the database and of the filesystem. It turns a
recording into a ``PosterQuery``, and a query into a ``PosterMatch`` (image
URLs, best first) or nothing. HTTP goes through a ``JsonClient`` so tests hand
in a fake instead of patching a network library.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

import aiohttp

from guide_metadata import GuideMetadata
from services.file_namer import FileNamer

logger = logging.getLogger(__name__)

KIND_TV = "tv"
KIND_MOVIE = "movie"
KIND_UNKNOWN = "unknown"

SOURCE_TMDB = "tmdb"
SOURCE_TVMAZE = "tvmaze"

TMDB_API_BASE = "https://api.themoviedb.org/3"
# w500 is 500x750, sharp as a library poster. w154 is what History shows: a
# thumbnail a few dozen pixels wide should not cost a full poster per row.
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_THUMBNAIL_BASE = "https://image.tmdb.org/t/p/w154"
TVMAZE_API_BASE = "https://api.tvmaze.com"

# These are not IPTV providers, so they are told who is really asking rather
# than being shown the player-style User-Agent providers get.
LOOKUP_USER_AGENT = "Mustarrd (poster lookup)"
LOOKUP_TIMEOUT = aiohttp.ClientTimeout(total=12, sock_connect=6, sock_read=8)
LOOKUP_MAX_JSON_BYTES = 2 * 1024 * 1024
# With nothing else to go on, a recording this long is more likely a film
# than an episode, so films are searched first. Padding is included.
LONG_FORM_MINUTES = 85

_MOVIE_CATEGORY_WORDS = ("movie", "film", "cinema")
_TMDB_TV_HINTS = ("tv", "series", "show")
_TMDB_MOVIE_HINTS = ("movie", "film")

_TRAILING_BRACKETS_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*$")
_LEADING_TAG_RE = re.compile(r"^\s*(?:new|live|repeat)\s*[:\-]\s*", re.IGNORECASE)
_YEAR_TAG_RE = re.compile(r"\(((?:19|20)\d{2})\)")
_IMDB_ID_RE = re.compile(r"tt\d{5,}")
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class PosterQuery:
    """What we know about a recording that a poster source can use."""

    title: str
    kind: str = KIND_UNKNOWN
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    # A "(1979)" the guide put in the title, used to tell remakes apart.
    year: Optional[int] = None
    runtime_minutes: Optional[int] = None


@dataclass(frozen=True)
class PosterMatch:
    """A poster a source found.

    ``urls`` is the full-size poster, best first; later ones are fallbacks.
    ``thumbnail_url`` is a small rendition of the same image, when the source
    offers one.
    """

    urls: Tuple[str, ...]
    source: str
    thumbnail_url: Optional[str] = None


def clean_lookup_title(title: Optional[str]) -> str:
    """The show or film name, without the decorations guides hang on it.

    ``"New: Bluey (2018) [HD]"`` and ``"Bluey S03E12 - Sleepytime"`` both
    come out as ``"Bluey"``.
    """
    text = str(title or "").strip()
    if not text:
        return ""
    text = FileNamer.extract_show_name(text)
    text = _LEADING_TAG_RE.sub("", text)
    while True:
        stripped = _TRAILING_BRACKETS_RE.sub("", text)
        if stripped == text or not stripped.strip():
            break
        text = stripped
    return text.strip()


def normalize_title(title: Optional[str]) -> str:
    """Reduce a title to what has to be equal for two titles to be the same show."""
    text = clean_lookup_title(title)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    # Letters and digits of any script. Keeping only a-z would reduce
    # "Новости 24" to "24" and let it match a show called "24".
    return "".join(ch for ch in text if ch.isalnum())


def _kind_for(title: str, metadata: GuideMetadata) -> str:
    if metadata.season_episode is not None or FileNamer.extract_season_episode(title or ""):
        return KIND_TV
    categories = " ".join(metadata.categories).casefold()
    if any(word in categories for word in _MOVIE_CATEGORY_WORDS):
        return KIND_MOVIE
    return KIND_UNKNOWN


def query_from_download(download) -> Optional[PosterQuery]:
    """Build the query for one recording, or ``None`` when it has no title.

    Duck-typed like ``nfo_writer.details_from_download``: anything carrying
    ``program_title`` and ``guide_metadata_json`` works.
    """
    payload: Dict[str, Any] = {}
    raw = getattr(download, "guide_metadata_json", None)
    if raw:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    metadata = GuideMetadata.from_guide_entry(payload)

    program_title = str(getattr(download, "program_title", "") or "").strip()
    title = clean_lookup_title(program_title)
    if not title and not (metadata.tmdb_id or metadata.tvdb_id or metadata.imdb_id):
        return None

    year_tag = _YEAR_TAG_RE.search(program_title)
    runtime = getattr(download, "duration_minutes", None)
    return PosterQuery(
        title=title,
        kind=_kind_for(program_title, metadata),
        tmdb_id=metadata.tmdb_id,
        tvdb_id=metadata.tvdb_id,
        imdb_id=metadata.imdb_id,
        year=int(year_tag.group(1)) if year_tag else None,
        runtime_minutes=runtime if isinstance(runtime, int) and runtime > 0 else None,
    )


# --- HTTP -----------------------------------------------------------------


class JsonClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any]:
        """GET ``url`` and return ``(status, parsed JSON or None)``."""


class AiohttpJsonClient:
    """The real ``JsonClient``. Bounded in time and in how much it will read."""

    async def get(self, url, *, params=None, headers=None):
        request_headers = {"User-Agent": LOOKUP_USER_AGENT, "Accept": "application/json"}
        request_headers.update(headers or {})
        async with aiohttp.ClientSession(
            timeout=LOOKUP_TIMEOUT, headers=request_headers
        ) as http_session:
            async with http_session.get(url, params=params, max_redirects=3) as response:
                if response.status != 200:
                    return response.status, None
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > LOOKUP_MAX_JSON_BYTES:
                        return response.status, None
                try:
                    return response.status, json.loads(bytes(body).decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return response.status, None


class FakeJsonClient:
    """Test adapter: answers from a table and records what it was asked.

    ``routes`` maps a URL, or a ``(url, params-as-sorted-tuple)`` pair, to a
    ``(status, payload)`` answer. Anything not in the table is a 404.
    """

    def __init__(self, routes: Optional[Dict[Any, Tuple[int, Any]]] = None) -> None:
        self.routes = dict(routes or {})
        self.calls: List[Tuple[str, Dict[str, str], Dict[str, str]]] = []

    async def get(self, url, *, params=None, headers=None):
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        keyed = (url, tuple(sorted(params.items())))
        if keyed in self.routes:
            return self.routes[keyed]
        if url in self.routes:
            return self.routes[url]
        return 404, None

    @property
    def urls(self) -> List[str]:
        return [call[0] for call in self.calls]


# --- sources --------------------------------------------------------------


def _first_dict(items: Any) -> Optional[dict]:
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                return item
    return None


def _tmdb_reference(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Split a guide's TMDB ID into ``(kind hint, number)``.

    Guides write it as ``1399``, ``tv/1399``, ``series/1399`` or ``movie/603``.
    """
    text = str(value or "").strip().casefold()
    match = _DIGITS_RE.search(text)
    if not match:
        return None, None
    prefix = text[: match.start()]
    if any(hint in prefix for hint in _TMDB_MOVIE_HINTS):
        return KIND_MOVIE, match.group(0)
    if any(hint in prefix for hint in _TMDB_TV_HINTS):
        return KIND_TV, match.group(0)
    return None, match.group(0)


def _imdb_reference(value: Optional[str]) -> Optional[str]:
    match = _IMDB_ID_RE.search(str(value or "").casefold())
    return match.group(0) if match else None


def _tvdb_reference(value: Optional[str]) -> Tuple[Optional[str], bool]:
    """``(number, is_episode_id)``. Guides write ``series/81189`` or ``episode/…``."""
    text = str(value or "").strip().casefold()
    match = _DIGITS_RE.search(text)
    if not match:
        return None, False
    return match.group(0), "episode" in text[: match.start()]


def _kinds_to_try(query: PosterQuery) -> Sequence[str]:
    """Which of TMDB's two catalogues to try first for this recording."""
    if query.kind == KIND_MOVIE:
        return (KIND_MOVIE, KIND_TV)
    if query.kind == KIND_UNKNOWN and (query.runtime_minutes or 0) >= LONG_FORM_MINUTES:
        return (KIND_MOVIE, KIND_TV)
    return (KIND_TV, KIND_MOVIE)


class TmdbSource:
    """TMDB, for users who have saved a key. Knows shows and films."""

    name = SOURCE_TMDB

    def __init__(self, api_key: str, http: JsonClient) -> None:
        self._api_key = (api_key or "").strip()
        self._http = http

    # TMDB hands out two credentials and people paste either: the short "API
    # key", sent as a query parameter, and the long "API Read Access Token",
    # sent as a bearer token.
    def _auth(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        if self._api_key.startswith("eyJ") or len(self._api_key) > 64:
            return {}, {"Authorization": f"Bearer {self._api_key}"}
        return {"api_key": self._api_key}, {}

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        auth_params, headers = self._auth()
        merged = dict(params or {})
        merged.update(auth_params)
        return await self._http.get(f"{TMDB_API_BASE}{path}", params=merged, headers=headers)

    async def check_key(self) -> Optional[bool]:
        """True if TMDB accepts the key, False if it refuses it, None if unreachable."""
        status, _payload = await self._get("/configuration")
        if status == 200:
            return True
        if status in (401, 403):
            return False
        return None

    @staticmethod
    def _poster(item: Optional[dict]) -> Optional[PosterMatch]:
        path = (item or {}).get("poster_path")
        if not isinstance(path, str) or not path.startswith("/"):
            return None
        return PosterMatch(
            urls=(f"{TMDB_IMAGE_BASE}{path}",),
            source=SOURCE_TMDB,
            thumbnail_url=f"{TMDB_THUMBNAIL_BASE}{path}",
        )

    @staticmethod
    def _names(item: dict) -> Iterable[str]:
        for key in ("name", "original_name", "title", "original_title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                yield value

    @classmethod
    def _same_name(cls, wanted: str, item: dict) -> bool:
        """An empty name matches nothing, so it can never match everything."""
        return bool(wanted) and wanted in {normalize_title(n) for n in cls._names(item)}

    async def find(self, query: PosterQuery) -> Optional[PosterMatch]:
        if not self._api_key:
            return None
        return (
            await self._by_tmdb_id(query)
            or await self._by_external_id(query)
            or await self._by_title(query)
        )

    async def _by_tmdb_id(self, query: PosterQuery) -> Optional[PosterMatch]:
        hint, number = _tmdb_reference(query.tmdb_id)
        if not number:
            return None
        wanted = normalize_title(query.title)
        for kind in ((hint,) if hint else _kinds_to_try(query)):
            status, item = await self._get(f"/{kind}/{number}")
            if status != 200 or not isinstance(item, dict):
                continue
            # TV 1399 and film 1399 are different things. A bare number is
            # only trusted when we know which it is, or the name agrees.
            certain = bool(hint) or query.kind == kind
            if not certain and not self._same_name(wanted, item):
                continue
            match = self._poster(item)
            if match:
                return match
        return None

    async def _by_external_id(self, query: PosterQuery) -> Optional[PosterMatch]:
        lookups = []
        imdb = _imdb_reference(query.imdb_id)
        if imdb:
            lookups.append((imdb, "imdb_id", False))
        tvdb, is_episode = _tvdb_reference(query.tvdb_id)
        if tvdb:
            lookups.append((tvdb, "tvdb_id", is_episode))

        for external_id, source, episode_only in lookups:
            status, payload = await self._get(
                f"/find/{external_id}", {"external_source": source}
            )
            if status != 200 or not isinstance(payload, dict):
                continue
            # TVDB numbers shows and episodes separately, so an episode's
            # number can also be some other show's. When the guide says it is
            # an episode, only the episode answer counts.
            for bucket in () if episode_only else ("tv_results", "movie_results"):
                match = self._poster(_first_dict(payload.get(bucket)))
                if match:
                    return match
            # The ID was for one episode: its poster is the show's poster.
            episode = _first_dict(payload.get("tv_episode_results"))
            show_id = (episode or {}).get("show_id")
            if show_id:
                status, show = await self._get(f"/tv/{show_id}")
                if status == 200:
                    match = self._poster(show if isinstance(show, dict) else None)
                    if match:
                        return match
        return None

    async def _by_title(self, query: PosterQuery) -> Optional[PosterMatch]:
        wanted = normalize_title(query.title)
        if not wanted:
            return None
        for kind in _kinds_to_try(query):
            searches = [{"query": query.title}]
            if query.year:
                # Narrow to the year first; guides get years wrong often
                # enough that the plain search stays as the fallback.
                year_param = "year" if kind == KIND_MOVIE else "first_air_date_year"
                searches.insert(0, {"query": query.title, year_param: str(query.year)})
            for params in searches:
                status, payload = await self._get(f"/search/{kind}", params)
                if status != 200 or not isinstance(payload, dict):
                    continue
                for item in payload.get("results") or []:
                    if not isinstance(item, dict) or not self._same_name(wanted, item):
                        continue
                    match = self._poster(item)
                    if match:
                        return match
        return None


class TvMazeSource:
    """TVMaze: free, no key, TV shows only."""

    name = SOURCE_TVMAZE

    def __init__(self, http: JsonClient) -> None:
        self._http = http

    @staticmethod
    def _poster(show: Optional[dict]) -> Optional[PosterMatch]:
        image = (show or {}).get("image")
        if not isinstance(image, dict):
            return None
        by_size = {}
        for size in ("original", "medium"):
            url = image.get(size)
            if isinstance(url, str) and url.startswith("http"):
                # TVMaze still hands out some http:// image links.
                by_size[size] = "https://" + url.split("://", 1)[1]
        if not by_size:
            return None
        return PosterMatch(
            urls=tuple(by_size.values()),
            source=SOURCE_TVMAZE,
            thumbnail_url=by_size.get("medium"),
        )

    async def find(self, query: PosterQuery) -> Optional[PosterMatch]:
        if query.kind == KIND_MOVIE:
            return None
        return await self._by_external_id(query) or await self._by_title(query)

    async def _by_external_id(self, query: PosterQuery) -> Optional[PosterMatch]:
        lookups = []
        tvdb, is_episode = _tvdb_reference(query.tvdb_id)
        if tvdb and not is_episode:
            lookups.append({"thetvdb": tvdb})
        imdb = _imdb_reference(query.imdb_id)
        if imdb:
            lookups.append({"imdb": imdb})

        for params in lookups:
            status, show = await self._http.get(f"{TVMAZE_API_BASE}/lookup/shows", params=params)
            if status != 200 or not isinstance(show, dict):
                continue
            match = self._poster(show)
            if match:
                return match
        return None

    async def _by_title(self, query: PosterQuery) -> Optional[PosterMatch]:
        wanted = normalize_title(query.title)
        if not wanted:
            return None
        status, results = await self._http.get(
            f"{TVMAZE_API_BASE}/search/shows", params={"q": query.title}
        )
        if status != 200 or not isinstance(results, list):
            return None
        for result in results:
            show = result.get("show") if isinstance(result, dict) else None
            if not isinstance(show, dict):
                continue
            if normalize_title(show.get("name")) != wanted:
                continue
            match = self._poster(show)
            if match:
                return match
        return None


def build_sources(tmdb_api_key: Optional[str], http: JsonClient) -> List[Any]:
    """The sources to ask, in order. TMDB only joins when there is a key."""
    sources: List[Any] = []
    if (tmdb_api_key or "").strip():
        sources.append(TmdbSource(tmdb_api_key, http))
    sources.append(TvMazeSource(http))
    return sources


async def resolve_poster(query: PosterQuery, sources: Sequence[Any]) -> Optional[PosterMatch]:
    """Ask each source in turn; the first poster wins.

    A source that is down, slow or answering nonsense is skipped rather than
    ending the search. The exception's text is not logged: for TMDB it can
    carry the request URL, and the request URL carries the user's key.
    """
    for source in sources:
        try:
            match = await source.find(query)
        except Exception as exc:  # noqa: BLE001 - one source failing is not the answer
            logger.warning(
                "Poster source %s failed (%s)", getattr(source, "name", source), type(exc).__name__
            )
            continue
        if match:
            return match
    return None
