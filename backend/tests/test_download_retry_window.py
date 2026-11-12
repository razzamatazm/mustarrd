"""
#442: the retry window that keeps a download alive across a provider outage is
a setting, not a constant.

The window is the total time a recording will spend waiting for the provider to
come back, and it is spent in growing waits (5s, 10s, 15s ... capped at 30s).
The default is worked out from the constants that used to be hard-coded, so an
install that never touches the setting retries exactly as before: three
retries, after 5, 10 and 15 seconds.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import aiohttp
from pydantic import ValidationError

from api.settings import SettingsUpdate
from database import _apply_lightweight_migrations
from models import AppSettings
from services.download_manager import DownloadManager, TRANSIENT_RETRY_LIMIT
from download_retry_window import (
    DEFAULT_RETRY_WINDOW_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    MAX_RETRY_WINDOW_SECONDS,
    resolve_retry_window_seconds,
    retry_delays_for_window,
)


class RetryScheduleTests(unittest.TestCase):
    def test_default_window_is_todays_three_retries(self):
        """5s + 10s + 15s: the schedule the hard-coded constants produced."""
        self.assertEqual(DEFAULT_RETRY_WINDOW_SECONDS, 30)
        self.assertEqual(retry_delays_for_window(DEFAULT_RETRY_WINDOW_SECONDS), [5.0, 10.0, 15.0])
        self.assertEqual(len(retry_delays_for_window(DEFAULT_RETRY_WINDOW_SECONDS)), TRANSIENT_RETRY_LIMIT)

    def test_zero_window_means_no_retries(self):
        self.assertEqual(retry_delays_for_window(0), [])

    def test_waits_add_up_to_exactly_the_window(self):
        for window in (30, 60, 120, 300, 600, 1800, MAX_RETRY_WINDOW_SECONDS):
            with self.subTest(window=window):
                self.assertEqual(sum(retry_delays_for_window(window)), window)

    def test_no_single_wait_exceeds_the_cap(self):
        """A long window buys more attempts, not longer gaps between them."""
        delays = retry_delays_for_window(1800)

        self.assertLessEqual(max(delays), MAX_RETRY_DELAY_SECONDS)
        self.assertGreater(len(delays), 50)

    def test_a_longer_window_starts_with_the_same_waits(self):
        self.assertEqual(retry_delays_for_window(600)[:3], [5.0, 10.0, 15.0])


class ResolveRetryWindowTests(unittest.TestCase):
    def test_valid_values_pass_through(self):
        for value in (0, 30, 600, MAX_RETRY_WINDOW_SECONDS):
            with self.subTest(value=value):
                self.assertEqual(resolve_retry_window_seconds(value), value)

    def test_missing_or_nonsense_values_fall_back_to_the_default(self):
        for value in (None, "", "abc", -1, MAX_RETRY_WINDOW_SECONDS + 1, True, MagicMock()):
            with self.subTest(value=value):
                self.assertEqual(
                    resolve_retry_window_seconds(value), DEFAULT_RETRY_WINDOW_SECONDS
                )


def _failing_then(responses_or_errors):
    """Mock aiohttp.ClientSession whose .get() yields each item in turn."""
    call_index = [0]

    def get_side_effect(*args, **kwargs):
        item = responses_or_errors[call_index[0]]
        call_index[0] += 1
        cm = MagicMock()
        if isinstance(item, BaseException):
            cm.__aenter__ = AsyncMock(side_effect=item)
        else:
            cm.__aenter__ = AsyncMock(return_value=item)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    http_session = MagicMock()
    http_session.get.side_effect = get_side_effect

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=http_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return http_session, client_cm


def _ok_response(body):
    response = MagicMock()
    response.status = 200
    response.content_length = len(body)
    response.reason = "OK"
    response.headers = {}

    async def iter_chunked(_size):
        yield body

    response.content.iter_chunked = iter_chunked
    return response


def _settings(window_seconds):
    settings = AppSettings()
    settings.download_retry_window_seconds = window_seconds
    return settings


class DownloadHonoursRetryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def _download(self, items, settings):
        http_session, client_cm = _failing_then(items)
        manager = DownloadManager()
        sleep = AsyncMock()
        self.http_session = http_session
        self.sleep = sleep

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.ts")
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch("services.download_manager.asyncio.sleep", sleep),
                patch.object(manager, "_load_app_settings", AsyncMock(return_value=settings)),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                return await manager._download_file(
                    "http://provider/stream", path, 1, AsyncMock(), offset=0
                )

    def _waits(self):
        return [call.args[0] for call in self.sleep.await_args_list]

    async def test_default_setting_retries_exactly_as_before(self):
        errors = [aiohttp.ClientConnectionError("refused") for _ in range(10)]

        with self.assertRaises(aiohttp.ClientConnectionError):
            await self._download(errors, _settings(DEFAULT_RETRY_WINDOW_SECONDS))

        self.assertEqual(self.http_session.get.call_count, 4, "Initial attempt plus three retries.")
        self.assertEqual(self._waits(), [5.0, 10.0, 15.0])

    async def test_unreadable_settings_fall_back_to_the_default(self):
        errors = [aiohttp.ClientConnectionError("refused") for _ in range(10)]

        with self.assertRaises(aiohttp.ClientConnectionError):
            await self._download(errors, None)

        self.assertEqual(self.http_session.get.call_count, 4)
        self.assertEqual(self._waits(), [5.0, 10.0, 15.0])

    async def test_longer_window_survives_an_outage_the_default_would_not(self):
        """Six failed attempts in a row used to end the recording at the fourth."""
        body = b"\x47" * 256
        items = [aiohttp.ClientConnectionError("refused") for _ in range(6)]
        items.append(_ok_response(body))

        total = await self._download(items, _settings(120))

        self.assertEqual(total, len(body))
        self.assertEqual(self.http_session.get.call_count, 7)
        self.assertEqual(self._waits(), [5.0, 10.0, 15.0, 20.0, 25.0, 30.0])

    async def test_longer_window_still_gives_up_once_it_is_spent(self):
        errors = [aiohttp.ClientConnectionError("refused") for _ in range(50)]

        with self.assertRaises(aiohttp.ClientConnectionError):
            await self._download(errors, _settings(60))

        self.assertEqual(sum(self._waits()), 60)
        self.assertEqual(self.http_session.get.call_count, len(self._waits()) + 1)

    async def test_zero_window_fails_on_the_first_network_error(self):
        errors = [aiohttp.ClientConnectionError("refused") for _ in range(3)]

        with self.assertRaises(aiohttp.ClientConnectionError):
            await self._download(errors, _settings(0))

        self.assertEqual(self.http_session.get.call_count, 1)
        self.assertEqual(self._waits(), [])

    async def test_a_clean_download_never_reads_the_setting(self):
        body = b"\x47" * 64
        http_session, client_cm = _failing_then([_ok_response(body)])
        manager = DownloadManager()
        load = AsyncMock(return_value=_settings(600))

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch.object(manager, "_load_app_settings", load),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                await manager._download_file(
                    "http://provider/stream", os.path.join(tmp, "out.ts"), 1, AsyncMock(), offset=0
                )

        load.assert_not_awaited()


class RetryWindowSettingTests(unittest.IsolatedAsyncioTestCase):
    def test_api_accepts_the_range_and_rejects_outside_it(self):
        for value in (0, 30, 600, MAX_RETRY_WINDOW_SECONDS):
            with self.subTest(value=value):
                model = SettingsUpdate(download_retry_window_seconds=value)
                self.assertEqual(model.download_retry_window_seconds, value)
        for value in (-1, MAX_RETRY_WINDOW_SECONDS + 1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    SettingsUpdate(download_retry_window_seconds=value)

    def test_settings_payload_reports_the_window(self):
        settings = _settings(300)

        self.assertEqual(settings.to_dict()["download_retry_window_seconds"], 300)

    def test_settings_payload_reports_the_default_when_unset(self):
        """A row that predates the column must not hand the UI a null."""
        settings = _settings(None)

        self.assertEqual(
            settings.to_dict()["download_retry_window_seconds"], DEFAULT_RETRY_WINDOW_SECONDS
        )

    async def test_migration_adds_the_column_defaulting_to_todays_behaviour(self):
        conn = AsyncMock()

        async def column_exists(_conn, table_name, column_name):
            return not (
                table_name == "app_settings"
                and column_name == "download_retry_window_seconds"
            )

        with patch("database._column_exists", side_effect=column_exists):
            await _apply_lightweight_migrations(conn)

        executed = [str(call.args[0]) for call in conn.execute.await_args_list]
        self.assertIn(
            "ALTER TABLE app_settings ADD COLUMN "
            "download_retry_window_seconds INTEGER DEFAULT 30",
            executed,
        )


if __name__ == "__main__":
    unittest.main()
