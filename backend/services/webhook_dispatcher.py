"""Fire a user-configured webhook when a recording changes state.

This is the first consumer of the recording event seam
(``services/recording_events.py``). It subscribes once at startup, and for
every event it looks up the URL the user configured for that event and POSTs
the event envelope to it as JSON.

Shape:

- **One URL per event, empty means off.** Five settings columns, one for each
  event the seam publishes. No list, no per-target CRUD.
- **No retries.** A delivery either works or is logged. A recording is never
  failed, delayed or retried because a webhook was unhappy — the whole point
  of the seam is that a subscriber cannot reach back into the pipeline.
- **The body is the event envelope**, ``RecordingEvent.to_dict()``, carrying a
  ``version`` so a receiver can tell when the shape changes. The recording
  block deliberately excludes ``source_url`` (it embeds provider
  credentials), which the seam already guarantees.

Address policy — see ``docs/adr/0005-webhook-targets-may-be-on-the-lan.md``.
The logo cache's SSRF guard requires a globally routable address; a webhook
cannot, because the common target *is* a private address: the Plex box, the
Jellyfin box, a Home Assistant instance, an arr container. So the guard here
keeps everything else — http/https only, no redirects followed, bounded
timeout, capped response — and blocks only the address ranges that are never
a legitimate webhook target and are the actual SSRF prize: link-local
(including the cloud metadata endpoint at 169.254.169.254), unspecified,
multicast and reserved.

``FakeWebhookSender`` is the test adapter, in the mould of
``FakeRecordingSubscriber``: hand it in as ``sender``, run the path, assert on
``calls``.
"""

import asyncio
import inspect
import ipaddress
import logging
import socket
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from database import async_session_maker
from services.recording_events import (
    POSTPROCESSING_COMPLETED,
    RECORDING_CANCELLED,
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    RecordingEvent,
)

logger = logging.getLogger(__name__)

# The events a user can hang a webhook off, in the order they appear in the UI.
WEBHOOK_EVENTS: Tuple[str, ...] = (
    RECORDING_STARTED,
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_CANCELLED,
    POSTPROCESSING_COMPLETED,
)

# event name -> the app_settings column holding its URL.
WEBHOOK_SETTING_FIELD_BY_EVENT: Dict[str, str] = {
    RECORDING_STARTED: "webhook_url_recording_started",
    RECORDING_COMPLETED: "webhook_url_recording_completed",
    RECORDING_FAILED: "webhook_url_recording_failed",
    RECORDING_CANCELLED: "webhook_url_recording_cancelled",
    POSTPROCESSING_COMPLETED: "webhook_url_postprocessing_completed",
}

WEBHOOK_SETTING_FIELDS: Tuple[str, ...] = tuple(
    WEBHOOK_SETTING_FIELD_BY_EVENT[event] for event in WEBHOOK_EVENTS
)

MAX_WEBHOOK_URL_LENGTH = 1000

# A webhook receiver that has not answered in ten seconds is not going to.
# Total is what protects the drain task; the finer limits fail faster on a
# black-holed address.
WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_connect=5, sock_read=8)

# Nothing downstream reads the response; this is only so a receiver that
# streams a large body cannot hold the drain task or the memory.
WEBHOOK_MAX_RESPONSE_BYTES = 64 * 1024

USER_AGENT = "Mustarrd-Webhook/1"


def setting_field_for_event(event_name: str) -> Optional[str]:
    """The app_settings column holding the URL for this event, if any."""
    return WEBHOOK_SETTING_FIELD_BY_EVENT.get(event_name)


class WebhookUrlError(ValueError):
    """A configured webhook URL that we refuse to send to."""


def _address_is_forbidden(address: "ipaddress._BaseAddress") -> bool:
    """Ranges that are never a webhook and are the reason SSRF guards exist.

    Private and loopback addresses are deliberately *allowed*: a webhook
    pointed at the Plex box on the LAN is the main use case.
    """
    return bool(
        address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def validate_webhook_url(url: Optional[str]) -> str:
    """Normalise a user-supplied webhook URL, or raise ``WebhookUrlError``.

    Empty (or whitespace) means "no webhook for this event" and comes back as
    an empty string rather than an error, so clearing the field in Settings
    turns the webhook off.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    if len(cleaned) > MAX_WEBHOOK_URL_LENGTH:
        raise WebhookUrlError(
            f"Webhook URL must be {MAX_WEBHOOK_URL_LENGTH} characters or fewer"
        )
    if any(character.isspace() for character in cleaned):
        raise WebhookUrlError("Webhook URL cannot contain spaces")

    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:
        raise WebhookUrlError("Webhook URL could not be parsed") from exc

    if parsed.scheme not in ("http", "https"):
        raise WebhookUrlError("Webhook URL must start with http:// or https://")

    hostname = parsed.hostname
    if not hostname:
        raise WebhookUrlError("Webhook URL must include a host")

    # A literal IP is checked here; a name is checked again after resolution,
    # just before the request goes out.
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and _address_is_forbidden(address):
        raise WebhookUrlError(
            "Webhook URL points at a reserved address that cannot be a webhook target"
        )

    return cleaned


async def resolve_and_check_host(hostname: str) -> bool:
    """Resolve the host and refuse if any address it answers with is forbidden."""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if _address_is_forbidden(address):
            return False
    return True


class AiohttpWebhookSender:
    """POSTs the payload as JSON. Follows no redirects, reads little, gives up fast."""

    async def __call__(self, url: str, payload: Dict[str, Any]) -> None:
        hostname = urlparse(url).hostname or ""
        if not await resolve_and_check_host(hostname):
            raise WebhookUrlError(
                f"Webhook host {hostname!r} did not resolve to an address we will send to"
            )

        async with aiohttp.ClientSession(timeout=WEBHOOK_TIMEOUT) as session:
            async with session.post(
                url,
                json=payload,
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                # Read a bounded amount so a chatty receiver cannot pin the
                # drain task, then drop it: nothing here reads the body.
                await response.content.read(WEBHOOK_MAX_RESPONSE_BYTES)
                if response.status >= 400:
                    raise WebhookDeliveryError(
                        f"webhook returned HTTP {response.status}"
                    )


class WebhookDeliveryError(RuntimeError):
    """The receiver answered, and said no."""


TargetLoader = Callable[[], Any]
WebhookSender = Callable[[str, Dict[str, Any]], Optional[Awaitable[None]]]


class WebhookDispatcher:
    """Subscriber on the recording event bus that turns events into POSTs.

    ``load_targets`` returns a mapping of ``app_settings`` field name to URL —
    the settings row's ``to_dict()`` will do. It may be sync or async, and it
    is called per event so a settings change takes effect immediately without
    a restart.
    """

    def __init__(
        self,
        load_targets: TargetLoader,
        sender: Optional[WebhookSender] = None,
    ) -> None:
        self._load_targets = load_targets
        self._sender: WebhookSender = sender or AiohttpWebhookSender()

    async def __call__(self, event: RecordingEvent) -> None:
        """Deliver one event. Never raises: a webhook is not the recording's problem."""
        field = setting_field_for_event(event.name)
        if field is None:
            return

        try:
            targets = await self._resolve_targets()
        except Exception as exc:  # noqa: BLE001 - settings trouble is not the recording's problem
            logger.warning(
                "Could not read webhook settings for %s; skipping webhook: %s",
                event.name,
                exc,
            )
            return

        raw_url = (targets or {}).get(field) or ""
        if not str(raw_url).strip():
            return

        try:
            url = validate_webhook_url(str(raw_url))
        except WebhookUrlError as exc:
            logger.warning(
                "Stored webhook URL for %s is not usable, skipping: %s",
                event.name,
                exc,
            )
            return

        try:
            result = self._sender(url, event.to_dict())
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - logged, never propagated
            logger.warning(
                "Webhook for %s failed (%s): %s",
                event.name,
                _redact(url),
                exc,
            )
        else:
            logger.info("Webhook for %s delivered to %s", event.name, _redact(url))

    async def _resolve_targets(self) -> Dict[str, Any]:
        result = self._load_targets()
        if inspect.isawaitable(result):
            result = await result
        return result or {}


async def load_webhook_targets() -> Dict[str, Any]:
    """Read the current webhook URLs straight from the settings row.

    Called per event rather than cached, so saving a URL in Settings takes
    effect on the next recording without a restart. Returns ``{}`` when no
    settings row exists yet, which reads as "every webhook off".
    """
    from sqlalchemy import select

    from models.settings import AppSettings

    async with async_session_maker() as session:
        result = await session.execute(select(AppSettings))
        settings = result.scalars().first()
        if settings is None:
            return {}
        return {field: getattr(settings, field, "") or "" for field in WEBHOOK_SETTING_FIELDS}


def register_webhook_subscriber(bus) -> WebhookDispatcher:
    """Put the webhook consumer on the recording event bus. Called at startup."""
    subscriber = WebhookDispatcher(load_targets=load_webhook_targets)
    bus.subscribe(subscriber)
    return subscriber


def _redact(url: str) -> str:
    """A webhook URL is often a bearer token in a path. Log the host only."""
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}/…"


class FakeWebhookSender:
    """Test adapter: records ``(url, payload)`` for every attempted delivery.

    ``raise_on`` is a URL that blows up when sent to, so the "a broken webhook
    never reaches the recording" promise can be tested.
    """

    def __init__(self, raise_on: Optional[str] = None) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.raise_on = raise_on

    async def __call__(self, url: str, payload: Dict[str, Any]) -> None:
        self.calls.append((url, payload))
        if self.raise_on is not None and url == self.raise_on:
            raise WebhookDeliveryError(f"receiver at {url} refused")

    @property
    def urls(self) -> List[str]:
        return [url for url, _ in self.calls]
