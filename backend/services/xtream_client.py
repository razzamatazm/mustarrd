import aiohttp
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlencode, quote


VOD_TIMEOUT = aiohttp.ClientTimeout(total=90)


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
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to get streams: HTTP {response.status}")
            data = await response.json(content_type=None)
            return list(data.values()) if isinstance(data, dict) else data

    async def get_epg(self, stream_id: str) -> list:
        """Get full EPG for a specific channel."""
        url = self._build_api_url("get_simple_data_table", stream_id=stream_id)
        session = await self._get_session()
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to get EPG: HTTP {response.status}")
            data = await response.json(content_type=None)
            return data.get("epg_listings") or []

    async def get_short_epg(self, stream_id: str, limit: int = 10) -> list:
        """Get short EPG (current + upcoming) for a channel."""
        url = self._build_api_url("get_short_epg", stream_id=stream_id, limit=limit)
        session = await self._get_session()
        async with session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Failed to get short EPG: HTTP {response.status}")
            data = await response.json(content_type=None)
            return data.get("epg_listings") or []

    async def get_xmltv(self) -> bytes:
        """Get XMLTV guide data."""
        url = f"{self.server_url}/xmltv.php?{urlencode({'username': self.username, 'password': self.password})}"
        session = await self._get_session()
        async with session.get(url) as response:
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
    ) -> str:
        """
        Build catchup/timeshift URL.

        Format: {server}/timeshift/{username}/{password}/{duration}/{YYYY-MM-DD:HH-MM}/{stream_id}.ts
        """
        raw_provider_start = (provider_start or "").strip()
        date_str = raw_provider_start or start_time.strftime("%Y-%m-%d:%H-%M")
        u = quote(self.username, safe="")
        p = quote(self.password, safe="")
        sid = quote(str(stream_id), safe="")
        return f"{self.server_url}/timeshift/{u}/{p}/{duration_minutes}/{date_str}/{sid}.ts"

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
