"""Disk-backed cache for channel logo images.

Provider logo URLs usually come from third-party hosts that send no cache
headers, so the browser refetches every logo on every page view. This
service fetches each logo once, stores the bytes under the config dir
(`logo_cache/`), and lets the API serve them with long-lived cache headers.

Because the fetch URL comes from provider channel data (and ultimately the
query string), the fetcher guards against SSRF: only http/https schemes,
hostnames must not resolve to private/loopback/link-local addresses, and
redirects are followed manually so every hop is re-checked.
"""

import asyncio
import hashlib
import ipaddress
import logging
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from config import ensure_config_files
from services.provider_http import provider_request_headers

logger = logging.getLogger(__name__)

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_MAX_REDIRECTS = 3
LOGO_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20, sock_connect=10, sock_read=15)
# Remember failed fetches briefly so a guide full of dead logo URLs doesn't
# hammer the provider host on every render.
NEGATIVE_CACHE_SECONDS = 15 * 60

_FALLBACK_CONTENT_TYPE = "application/octet-stream"


def _cache_dir(name: str = "logo_cache") -> Path:
    cache_dir = ensure_config_files() / name
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _host_is_public(host: str) -> bool:
    """Resolve the host and require every address to be globally routable."""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


async def _validate_logo_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    return await _host_is_public(parsed.hostname)


class LogoCache:
    # Subclasses (services/poster_art.py) keep a different kind of image in a
    # folder of their own, with their own size cap and their own way of
    # introducing themselves. Everything else - the disk cache, the SSRF
    # guard on every hop, the negative cache - is shared.
    cache_subdir = "logo_cache"
    max_bytes = LOGO_MAX_BYTES

    def __init__(self):
        # url hash -> in-flight fetch, so concurrent requests for the same
        # logo (a fresh guide render fires dozens at once) share one fetch.
        self._inflight: dict[str, asyncio.Future] = {}
        self._negative: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get_logo(self, url: str) -> tuple[bytes, str] | None:
        """Return (bytes, content_type) for the logo, fetching it on a miss."""
        return await self.get_image(url)

    def cached_image(self, url: str) -> tuple[bytes, str] | None:
        """The image if it is already on disk. Never touches the network."""
        return self._read_cached(_cache_key(url))

    async def get_image(self, url: str) -> tuple[bytes, str] | None:
        """Return (bytes, content_type) for the image, fetching it on a miss."""
        key = _cache_key(url)

        cached = self._read_cached(key)
        if cached is not None:
            return cached

        expiry = self._negative.get(key)
        if expiry is not None:
            if expiry > time.monotonic():
                return None
            del self._negative[key]

        async with self._lock:
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.ensure_future(self._fetch_and_store(url, key))
                self._inflight[key] = future
                future.add_done_callback(lambda _f: self._inflight.pop(key, None))

        try:
            return await asyncio.shield(future)
        except Exception:
            logger.exception("Image fetch failed cache=%s url_hash=%s", self.cache_subdir, key)
            self._negative[key] = time.monotonic() + NEGATIVE_CACHE_SECONDS
            return None

    def forget(self, url: str) -> None:
        """Drop everything remembered about ``url`` so the next request refetches."""
        key = _cache_key(url)
        self._negative.pop(key, None)
        try:
            cache_dir = _cache_dir(self.cache_subdir)
            for suffix in (".img", ".ct"):
                (cache_dir / f"{key}{suffix}").unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not clear cached image url_hash=%s", key, exc_info=True)

    def _open_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=LOGO_FETCH_TIMEOUT, headers=provider_request_headers()
        )

    def _read_cached(self, key: str) -> tuple[bytes, str] | None:
        cache_dir = _cache_dir(self.cache_subdir)
        image_path = cache_dir / f"{key}.img"
        if not image_path.exists():
            return None
        try:
            content = image_path.read_bytes()
            type_path = cache_dir / f"{key}.ct"
            content_type = (
                type_path.read_text(encoding="utf-8").strip()
                if type_path.exists()
                else _FALLBACK_CONTENT_TYPE
            )
            return content, content_type
        except OSError:
            return None

    async def _fetch_and_store(self, url: str, key: str) -> tuple[bytes, str] | None:
        if not await _validate_logo_url(url):
            self._negative[key] = time.monotonic() + NEGATIVE_CACHE_SECONDS
            return None

        async with self._open_session() as http_session:
            current_url = url
            for _hop in range(LOGO_MAX_REDIRECTS + 1):
                async with http_session.get(current_url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            break
                        current_url = urljoin(current_url, location)
                        if not await _validate_logo_url(current_url):
                            self._negative[key] = time.monotonic() + NEGATIVE_CACHE_SECONDS
                            return None
                        continue

                    if response.status != 200:
                        break

                    declared = response.content_length
                    if declared is not None and declared > self.max_bytes:
                        break

                    content = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        content.extend(chunk)
                        if len(content) > self.max_bytes:
                            self._negative[key] = time.monotonic() + NEGATIVE_CACHE_SECONDS
                            return None

                    if not content:
                        break

                    content_type = response.headers.get("Content-Type", "")
                    content_type = content_type.split(";")[0].strip().lower()
                    if not content_type.startswith("image/"):
                        content_type = _FALLBACK_CONTENT_TYPE

                    self._write_cached(key, bytes(content), content_type)
                    return bytes(content), content_type

        self._negative[key] = time.monotonic() + NEGATIVE_CACHE_SECONDS
        return None

    def _write_cached(self, key: str, content: bytes, content_type: str) -> None:
        cache_dir = _cache_dir(self.cache_subdir)
        try:
            # Write-then-rename so a crash mid-write never leaves a truncated
            # image that would be served as-is forever.
            tmp_path = cache_dir / f"{key}.img.tmp"
            tmp_path.write_bytes(content)
            tmp_path.rename(cache_dir / f"{key}.img")
            (cache_dir / f"{key}.ct").write_text(content_type, encoding="utf-8")
        except OSError:
            logger.warning("Could not persist cached logo url_hash=%s", key, exc_info=True)


logo_cache = LogoCache()
