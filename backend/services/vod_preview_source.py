"""Short-lived grants for the VOD preview source relay.

A VOD preview needs FFmpeg to *seek* — a two-hour film is useless if you can
only ever watch its first minutes — and seeking means FFmpeg must be able to
issue byte-range requests against the source itself. It cannot be given the
provider URL to do that with: the URL embeds the account username and
password, and anything in FFmpeg's argv is readable by every user on the host
via `ps`.

So the provider URL stays here, behind an opaque token, and FFmpeg is pointed
at a loopback relay endpoint that carries the token and nothing else. The
token is:

- unguessable (256 bits from `secrets`), so it is not a credential anyone can
  reach by scanning;
- short-lived, so a leaked one dies with the session that minted it;
- revoked the moment its preview session is torn down.

The relay endpoint additionally refuses non-loopback callers. The token alone
is not the security boundary — it is what keeps two concurrent previews from
reading each other's source.
"""

import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import quote


# Long enough to outlive the wall-clock ceiling on a preview session plus the
# teardown grace after it, so FFmpeg never has a grant expire underneath it.
# Grants are revoked on session close, so this only bounds the leak window of
# a session that died without cleaning up.
GRANT_TTL_SECONDS = 20 * 60


@dataclass(frozen=True)
class _Grant:
    url: str
    expires_at: float


class VodPreviewSourceRelay:
    """The token -> provider URL table behind the loopback relay endpoint."""

    def __init__(self, ttl_seconds: float = GRANT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._grants: Dict[str, _Grant] = {}

    def mint(self, url: str) -> str:
        """Register a provider URL and return the token that stands in for it."""
        self._prune()
        token = secrets.token_urlsafe(32)
        self._grants[token] = _Grant(url=url, expires_at=time.monotonic() + self._ttl)
        return token

    def resolve(self, token: str) -> Optional[str]:
        """The provider URL for a live token, or None if unknown or expired."""
        grant = self._grants.get(token)
        if grant is None:
            return None
        if grant.expires_at <= time.monotonic():
            self._grants.pop(token, None)
            return None
        return grant.url

    def revoke(self, token: str) -> None:
        """Drop a grant. Tolerates being called more than once: preview
        teardown paths are deliberately idempotent."""
        self._grants.pop(token, None)

    def _prune(self) -> None:
        now = time.monotonic()
        for token, grant in list(self._grants.items()):
            if grant.expires_at <= now:
                del self._grants[token]

    @property
    def active_count(self) -> int:
        return len(self._grants)


def loopback_source_url(port: int, token: str) -> str:
    """The URL FFmpeg is given. Loopback-only and credential-free by
    construction — see the module docstring for why that matters."""
    return f"http://127.0.0.1:{port}/api/vod/preview/source/{quote(token, safe='')}"


vod_preview_source_relay = VodPreviewSourceRelay()
