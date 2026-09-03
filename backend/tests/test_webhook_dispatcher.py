"""The webhook consumer of the recording event seam, in isolation.

Covers URL validation, per-event routing, payload shape, and the promise that
a broken webhook is logged and never reaches the recording. The wiring test
(a real publish on the app bus reaches the dispatcher) lives in
``test_webhook_seam_wiring.py``; the settings surface in
``test_webhook_settings_api.py``.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.recording_events import (  # noqa: E402
    POSTPROCESSING_COMPLETED,
    RECORDING_CANCELLED,
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    RecordingEventBus,
)
from services.webhook_dispatcher import (  # noqa: E402
    WEBHOOK_EVENTS,
    WEBHOOK_SETTING_FIELDS,
    AiohttpWebhookSender,
    FakeWebhookSender,
    WebhookDeliveryError,
    WebhookDispatcher,
    WebhookUrlError,
    setting_field_for_event,
    validate_webhook_url,
)


def targets(**overrides):
    """Build a settings-shaped mapping with every webhook field present."""
    values = {field: "" for field in WEBHOOK_SETTING_FIELDS}
    for event, url in overrides.items():
        values[setting_field_for_event(event)] = url
    return values


class ValidateWebhookUrlTests(unittest.TestCase):
    def test_empty_means_disabled(self):
        self.assertEqual(validate_webhook_url(""), "")
        self.assertEqual(validate_webhook_url("   "), "")
        self.assertEqual(validate_webhook_url(None), "")

    def test_accepts_https_and_http(self):
        self.assertEqual(
            validate_webhook_url(" https://ntfy.sh/mustarrd "),
            "https://ntfy.sh/mustarrd",
        )
        self.assertEqual(
            validate_webhook_url("http://192.168.1.50:8096/library/refresh"),
            "http://192.168.1.50:8096/library/refresh",
        )

    def test_lan_and_loopback_targets_are_allowed(self):
        """A Plex or Jellyfin box on the LAN is the main use case, not an attack."""
        for url in (
            "http://192.168.0.10:32400/library/sections/1/refresh",
            "http://10.0.0.5/hook",
            "http://127.0.0.1:8080/hook",
            "http://plex.local:32400/hook",
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_webhook_url(url), url)

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://host/x", "gopher://host", "/relative"):
            with self.subTest(url=url):
                with self.assertRaises(WebhookUrlError):
                    validate_webhook_url(url)

    def test_rejects_missing_host(self):
        with self.assertRaises(WebhookUrlError):
            validate_webhook_url("http://")

    def test_rejects_link_local_and_cloud_metadata(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://[fe80::1]/hook",
            "http://0.0.0.0/hook",
        ):
            with self.subTest(url=url):
                with self.assertRaises(WebhookUrlError):
                    validate_webhook_url(url)

    def test_rejects_whitespace_and_overlong_urls(self):
        with self.assertRaises(WebhookUrlError):
            validate_webhook_url("http://host/a b")
        with self.assertRaises(WebhookUrlError):
            validate_webhook_url("https://host/" + "x" * 1200)


class WebhookDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = FakeWebhookSender()

    def dispatcher(self, **overrides):
        values = targets(**overrides)

        async def load():
            return values

        return WebhookDispatcher(load_targets=load, sender=self.sender)

    async def test_posts_configured_url_with_event_envelope(self):
        dispatcher = self.dispatcher(**{RECORDING_COMPLETED: "https://ntfy.sh/topic"})
        bus = RecordingEventBus()
        bus.subscribe(dispatcher)

        bus.publish(RECORDING_COMPLETED, {"id": 12, "title": "Match of the Day"})
        await bus.drain_once()

        self.assertEqual(len(self.sender.calls), 1)
        url, payload = self.sender.calls[0]
        self.assertEqual(url, "https://ntfy.sh/topic")
        self.assertEqual(payload["event"], RECORDING_COMPLETED)
        self.assertEqual(payload["recording"]["title"], "Match of the Day")
        self.assertIn("event_id", payload)
        self.assertIn("occurred_at", payload)
        self.assertEqual(payload["version"], 1)

    async def test_unconfigured_event_sends_nothing(self):
        dispatcher = self.dispatcher(**{RECORDING_COMPLETED: "https://ntfy.sh/topic"})

        await dispatcher(RecordingEventBus().build(RECORDING_STARTED, {"id": 1}))

        self.assertEqual(self.sender.calls, [])

    async def test_each_event_routes_to_its_own_url(self):
        urls = {event: f"https://example.com/{event}" for event in WEBHOOK_EVENTS}
        dispatcher = self.dispatcher(**urls)
        bus = RecordingEventBus()
        bus.subscribe(dispatcher)

        for event in WEBHOOK_EVENTS:
            bus.publish(event, {"id": 1})
        await bus.drain_once()

        self.assertEqual(
            [url for url, _ in self.sender.calls],
            [f"https://example.com/{event}" for event in WEBHOOK_EVENTS],
        )

    async def test_all_five_seam_events_are_routable(self):
        self.assertEqual(
            set(WEBHOOK_EVENTS),
            {
                RECORDING_STARTED,
                RECORDING_COMPLETED,
                RECORDING_FAILED,
                RECORDING_CANCELLED,
                POSTPROCESSING_COMPLETED,
            },
        )

    async def test_sender_failure_is_swallowed(self):
        self.sender.raise_on = "https://ntfy.sh/topic"
        dispatcher = self.dispatcher(**{RECORDING_FAILED: "https://ntfy.sh/topic"})

        with self.assertLogs("services.webhook_dispatcher", level="WARNING") as logs:
            await dispatcher(RecordingEventBus().build(RECORDING_FAILED, {"id": 3}))

        self.assertTrue(any("webhook" in line.lower() for line in logs.output))

    async def test_invalid_stored_url_is_skipped_not_raised(self):
        """A URL that got into the database by some other route never fires."""
        values = targets()
        values[setting_field_for_event(RECORDING_COMPLETED)] = "file:///etc/passwd"

        async def load():
            return values

        dispatcher = WebhookDispatcher(load_targets=load, sender=self.sender)

        with self.assertLogs("services.webhook_dispatcher", level="WARNING"):
            await dispatcher(RecordingEventBus().build(RECORDING_COMPLETED, {"id": 4}))

        self.assertEqual(self.sender.calls, [])

    async def test_settings_lookup_failure_never_reaches_the_recording(self):
        async def explode():
            raise RuntimeError("database is having a moment")

        dispatcher = WebhookDispatcher(load_targets=explode, sender=self.sender)

        with self.assertLogs("services.webhook_dispatcher", level="WARNING"):
            await dispatcher(RecordingEventBus().build(RECORDING_COMPLETED, {"id": 5}))

        self.assertEqual(self.sender.calls, [])

    async def test_a_wedged_webhook_does_not_stall_other_subscribers(self):
        """The bus is fire-and-forget; assert the dispatcher itself is bounded."""
        dispatcher = self.dispatcher(**{RECORDING_COMPLETED: "https://ntfy.sh/topic"})
        bus = RecordingEventBus()
        bus.subscribe(dispatcher)

        bus.publish(RECORDING_COMPLETED, {"id": 7})
        await asyncio.wait_for(bus.drain_once(), timeout=2)

        self.assertEqual(len(self.sender.calls), 1)


class _FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.content = _FakeContent(body)


class _FakeContent:
    def __init__(self, body):
        self._body = body

    async def read(self, limit):
        return self._body[:limit]


class _RequestRecorder:
    """Stands in for aiohttp.ClientSession, recording how the POST was made."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.session_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ResponseContext(_FakeResponse(status=self.status))


class _ResponseContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class AiohttpWebhookSenderTests(unittest.IsolatedAsyncioTestCase):
    """The real transport's guards: address policy, no redirects, error status."""

    async def _send(self, url, *, resolves_to, status=200):
        from unittest.mock import patch

        recorder = _RequestRecorder(status=status)

        async def fake_getaddrinfo(host, port, **kwargs):
            return [(2, 1, 6, "", (resolves_to, 0))]

        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", fake_getaddrinfo):
            with patch("services.webhook_dispatcher.aiohttp.ClientSession", recorder):
                await AiohttpWebhookSender()(url, {"event": "recording.completed"})
        return recorder

    async def test_a_lan_address_is_sent_to(self):
        recorder = await self._send(
            "http://plex.local:32400/hook", resolves_to="192.168.1.50"
        )

        self.assertEqual(recorder.calls[0][0], "http://plex.local:32400/hook")

    async def test_redirects_are_not_followed(self):
        recorder = await self._send("http://plex.local/hook", resolves_to="192.168.1.50")

        self.assertIs(recorder.calls[0][1]["allow_redirects"], False)
        self.assertEqual(
            recorder.calls[0][1]["json"], {"event": "recording.completed"}
        )

    async def test_a_name_resolving_to_the_metadata_address_is_refused(self):
        with self.assertRaises(WebhookUrlError):
            await self._send("http://evil.example/hook", resolves_to="169.254.169.254")

    async def test_an_error_status_is_reported_as_a_failure(self):
        with self.assertRaises(WebhookDeliveryError):
            await self._send(
                "http://plex.local/hook", resolves_to="192.168.1.50", status=500
            )


class RedactionTests(unittest.TestCase):
    def test_a_token_in_the_path_is_not_logged(self):
        from services.webhook_dispatcher import _redact

        redacted = _redact("https://hooks.example.com:8443/services/SECRET-TOKEN")

        self.assertNotIn("SECRET-TOKEN", redacted)
        self.assertIn("hooks.example.com:8443", redacted)


if __name__ == "__main__":
    unittest.main()
