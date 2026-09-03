import aiohttp
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlencode, quote


VOD_TIMEOUT = aiohttp.ClientTimeout(total=90)
# Full channel list and per-channel EPG backfill responses are as heavy as VOD
# lists on large providers; the 30s session default times them out.
LIST_TIMEOUT = aiohttp.ClientTimeout(total=90)
XMLTV_TIMEOUT = aiohttp.ClientTimeout(total=300)

# The two shapes Xtream Codes providers serve catchup in. TIMESHIFT_STYLE_PATH
# is the historical form and stays the default everywhere.
TIMESHIFT_STYLE_PATH = "path"
TIMESHIFT_STYLE_QUERY = "query"
# The per-account setting adds "auto", which means "probe, then remember".
TIMESHIFT_STYLE_AUTO = "auto"
TIMESHIFT_STYLE_SETTINGS = (TIMESHIFT_STYLE_AUTO, TIMESHIFT_STYLE_PATH, TIMESHIFT_STYLE_QUERY)

_TIMESHIFT_PATH_MARKER = "/timeshift/"
_TIMESHIFT_QUERY_MARKER = "/streaming/timeshift.php"


def _format_timeshift_url(server_url, username, password, stream_id, start, duration, style) -> str:
    """The single place either catchup URL shape is spelled out.

    Every component arrives already percent-encoded, so this only assembles.
    Anything other than "query" gets the path form, which keeps an unrecognised
    style from taking an account off the historical behaviour.
    """
    if (style or "").strip().lower() == TIMESHIFT_STYLE_QUERY:
        return (
            f"{server_url}{_TIMESHIFT_QUERY_MARKER}"
            f"?username={username}&password={password}&stream={stream_id}"
            f"&start={start}&duration={duration}"
        )
    return f"{server_url}{_TIMESHIFT_PATH_MARKER}{username}/{password}/{duration}/{start}/{stream_id}.ts"


def _known_style(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().lower()
    return normalized if normalized in (TIMESHIFT_STYLE_PATH, TIMESHIFT_STYLE_QUERY) else None


def resolve_timeshift_style(account) -> str:
    """
    Decide which URL form to send for an account.

    An explicit "path" or "query" choice always wins. "auto" (and any value we
    do not recognise, including a missing one) uses whatever probing last
    settled on, and falls back to the historical path form when nothing has.

    Takes the account rather than its two style columns because they are never
    meaningful apart: the choice only means anything alongside what probing
    found.
    """
    return (
        _known_style(account.catchup_url_style)
        or _known_style(account.catchup_url_style_resolved)
        or TIMESHIFT_STYLE_PATH
    )


def timeshift_style_is_automatic(account) -> bool:
    """True when the account probes rather than being pinned to one form."""
    return _known_style(account.catchup_url_style) is None


def timeshift_style_of_url(url: Optional[str]) -> Optional[str]:
    """Which form an already-built URL is in, or None if it is not catchup."""
    if not _split_timeshift_url(url or ""):
        return None
    base = (url or "").partition("?")[0]
    return (
        TIMESHIFT_STYLE_QUERY
        if base.endswith(_TIMESHIFT_QUERY_MARKER)
        else TIMESHIFT_STYLE_PATH
    )


def other_timeshift_style(style: str) -> str:
    """The form to try when `style` is refused."""
    return (
        TIMESHIFT_STYLE_PATH
        if (style or "").strip().lower() == TIMESHIFT_STYLE_QUERY
        else TIMESHIFT_STYLE_QUERY
    )


def _split_timeshift_url(url: str) -> Optional[dict]:
    """
    Pull an already-built catchup URL back apart, in either form.

    Values come back still percent-encoded, so re-emitting them is a lossless
    move between the two forms. Returns None for anything that is not a catchup
    URL we recognise, which is the caller's signal to leave it alone.
    """
    if not url:
        return None
    base, _, query = url.partition("?")
    if base.endswith(_TIMESHIFT_QUERY_MARKER):
        params = {}
        for pair in query.split("&"):
            key, sep, value = pair.partition("=")
            if sep:
                params[key] = value
        required = ("username", "password", "stream", "start", "duration")
        if not all(key in params for key in required):
            return None
        return {
            "server_url": base[: -len(_TIMESHIFT_QUERY_MARKER)],
            "username": params["username"],
            "password": params["password"],
            "stream_id": params["stream"],
            "start": params["start"],
            "duration": params["duration"],
        }

    marker_at = base.rfind(_TIMESHIFT_PATH_MARKER)
    if marker_at == -1 or query:
        return None
    tail = base[marker_at + len(_TIMESHIFT_PATH_MARKER):]
    parts = tail.split("/")
    if len(parts) != 5:
        return None
    username, password, duration, start, stream_file = parts
    stream_id, dot, _extension = stream_file.rpartition(".")
    if not dot:
        return None
    return {
        "server_url": base[:marker_at],
        "username": username,
        "password": password,
        "stream_id": stream_id,
        "start": start,
        "duration": duration,
    }


def restyle_timeshift_url(url: Optional[str], style: str) -> Optional[str]:
    """
    Re-emit an existing catchup URL in the requested form.

    Used by the fallback: the download row already holds a built URL, and the
    retry needs the same recording in the other form without rebuilding it from
    the program. Returns None when the URL is not a catchup URL.
    """
    parts = _split_timeshift_url(url or "")
    if not parts:
        return None
    return _format_timeshift_url(style=style, **parts)


class XtreamClient:
    def __init__(self, server_url: str, username: str, password: str):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_api_url(self, action: str, **params) -> str:
        base = f"{self.server_url}/player_api.php"
        query = {"username": self.username, "password": self.password, "action": action}
        for key, value in params.items():
            if value is not None:
                query[key] = value
        return f"{base}?{urlencode(query)}"

    async def authenticate(self) -> dict:
        """Get server info and validate credentials."""
        url = self._build_api_url("")
        session = await self._get_session()
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Authentication failed: HTTP {response.status}")
            data = await response.json(content_type=None)
            if "user_info" not in data:
                raise Exception("Invalid response from server")
            if not data["user_info"].get("auth", 1):
                raise Exception("Invalid credentials")
            return data

    async def get_live_categories(self) -> list:
        """Get list of live TV categories."""
        url = self._build_api_url("get_live_categories")
        session = await self._get_session()
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to get categories: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_live_streams(self, category_id: Optional[str] = None) -> list:
        """Get list of live streams/channels."""
        url = self._build_api_url("get_live_streams", category_id=category_id)
        session = await self._get_session()
        async with session.get(url, timeout=LIST_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get streams: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_epg(self, stream_id: str) -> list:
        """Get full EPG for a specific channel."""
        url = self._build_api_url("get_simple_data_table", stream_id=stream_id)
        session = await self._get_session()
        async with session.get(url, timeout=LIST_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get EPG: HTTP {response.status}")
            data = await response.json(content_type=None)
            if isinstance(data, list):
                return data
            listings = data.get("epg_listings") or []
            if isinstance(listings, dict):
                listings = list(listings.values())
            return listings

    async def get_short_epg(self, stream_id: str, limit: int = 10) -> list:
        """Get short EPG (current + upcoming) for a channel."""
        url = self._build_api_url("get_short_epg", stream_id=stream_id, limit=limit)
        session = await self._get_session()
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to get short EPG: HTTP {response.status}")
            data = await response.json(content_type=None)
            if isinstance(data, list):
                return data
            listings = data.get("epg_listings") or []
            if isinstance(listings, dict):
                listings = list(listings.values())
            return listings

    async def get_xmltv(self) -> bytes:
        """Get XMLTV guide data."""
        url = f"{self.server_url}/xmltv.php?{urlencode({'username': self.username, 'password': self.password})}"
        session = await self._get_session()
        async with session.get(url, timeout=XMLTV_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get XMLTV: HTTP {response.status}")
            return await response.read()

    async def get_vod_categories(self) -> list:
        """Get VOD (movies) categories."""
        url = self._build_api_url("get_vod_categories")
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get VOD categories: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_vod_streams(self, category_id: Optional[str] = None) -> list:
        """Get VOD (movies) streams."""
        url = self._build_api_url("get_vod_streams", category_id=category_id)
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get VOD streams: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_vod_info(self, vod_id: str) -> dict:
        """Get VOD (movie) details."""
        url = self._build_api_url("get_vod_info", vod_id=vod_id)
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get VOD info: HTTP {response.status}")
            return await response.json(content_type=None)

    async def get_series_categories(self) -> list:
        """Get series categories."""
        url = self._build_api_url("get_series_categories")
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get series categories: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_series(self, category_id: Optional[str] = None) -> list:
        """Get series list."""
        url = self._build_api_url("get_series", category_id=category_id)
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get series: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_series_info(self, series_id: str) -> dict:
        """Get series details (seasons + episodes)."""
        url = self._build_api_url("get_series_info", series_id=series_id)
        session = await self._get_session()
        async with session.get(url, timeout=VOD_TIMEOUT) as response:
            if response.status != 200:
                raise Exception(f"Failed to get series info: HTTP {response.status}")
            return await response.json(content_type=None)

    def build_timeshift_url(
        self,
        stream_id: str,
        start_time: datetime,
        duration_minutes: int,
        provider_start: Optional[str] = None,
        style: Optional[str] = None,
    ) -> str:
        """
        Build a catchup/timeshift URL in one of the two forms providers serve.

        path  (default): {server}/timeshift/{username}/{password}/{duration}/{start}/{stream_id}.ts
        query:           {server}/streaming/timeshift.php?username=&password=&stream=&start=&duration=

        Both forms carry identical start tokens and durations. Anything other
        than "query" is treated as the path form, so an unrecognised value can
        never take an account off the historical behaviour.
        """
        raw_provider_start = (provider_start or "").strip()
        date_str = raw_provider_start or start_time.strftime("%Y-%m-%d:%H-%M")
        return _format_timeshift_url(
            self.server_url,
            quote(self.username, safe=""),
            quote(self.password, safe=""),
            quote(str(stream_id), safe=""),
            # ":" and "-" are legal in a query value and providers expect the
            # start token to read back exactly as they published it.
            quote(date_str, safe=":-"),
            duration_minutes,
            style,
        )

    def build_stream_url(self, stream_id: str, extension: str = "ts") -> str:
        """Build live stream URL."""
        u = quote(self.username, safe="")
        p = quote(self.password, safe="")
        sid = quote(str(stream_id), safe="")
        return f"{self.server_url}/live/{u}/{p}/{sid}.{extension}"

    def build_vod_url(self, vod_id: str, extension: Optional[str] = None) -> str:
        """Build VOD movie URL."""
        ext = (extension or "mp4").lstrip(".") or "mp4"
        u = quote(self.username, safe="")
        p = quote(self.password, safe="")
        vid = quote(str(vod_id), safe="")
        return f"{self.server_url}/movie/{u}/{p}/{vid}.{ext}"

    def build_series_url(self, episode_id: str, extension: Optional[str] = None) -> str:
        """Build VOD series episode URL."""
        ext = (extension or "mp4").lstrip(".") or "mp4"
        u = quote(self.username, safe="")
        p = quote(self.password, safe="")
        eid = quote(str(episode_id), safe="")
        return f"{self.server_url}/series/{u}/{p}/{eid}.{ext}"
