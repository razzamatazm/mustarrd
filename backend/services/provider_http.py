"""How Mustarrd introduces itself to an IPTV provider.

Left alone, aiohttp announces every request as ``Python/3.x aiohttp/3.x``.
Some providers drop the connection for anything that does not look like a
media player, so an account whose credentials work in every other app fails
here with a connection reset before a single byte comes back (#453).

Every request that goes to a provider - the Xtream API, recording downloads,
previews, and the logos a provider's playlist points at - sends the same
player-style User-Agent instead. ``CATCHUP_PROVIDER_USER_AGENT`` replaces it
for a provider that wants a specific one.

FFmpeg is not covered here because it never opens a provider URL: previews
hand it bytes on stdin or a loopback relay, and both of those fetch through
this module's headers.
"""

from typing import Dict

from config import settings as app_settings

# VLC is the player nearly every Xtream provider documents and tests against.
DEFAULT_PROVIDER_USER_AGENT = "VLC/3.0.21 LibVLC/3.0.21"


def provider_user_agent() -> str:
    """The User-Agent to send to providers, read fresh so tests and env agree.

    A blank value, or one carrying a line break (which could smuggle a second
    header into the request), falls back to the default rather than going out
    on the wire.
    """
    configured = str(getattr(app_settings, "provider_user_agent", "") or "").strip()
    if not configured or "\r" in configured or "\n" in configured:
        return DEFAULT_PROVIDER_USER_AGENT
    return configured


def provider_request_headers() -> Dict[str, str]:
    """Default headers for an ``aiohttp.ClientSession`` that talks to a provider."""
    return {"User-Agent": provider_user_agent()}
