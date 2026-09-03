"""Scheduled catchup downloads wait for the provider's archive to settle."""

import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from database import _apply_lightweight_migrations
from models import AppSettings, ScheduledRecording, ScheduledStatus
from services.scheduled_manager import ScheduledManager


def _make_schedule(minutes_since_end: int) -> ScheduledRecording:
    end = datetime.now(timezone.utc) - timedelta(minutes=minutes_since_end)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        id=42,
        account_id=1,
        channel_id="101",
        channel_name="Test Channel",
        program_title="Test Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        pre_padding_minutes=0,
        post_padding_minutes=0,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _session_maker(schedule, delay_minutes):
    schedules_result = MagicMock()
    schedules_result.scalars.return_value.all.return_value = [schedule]

    settings = AppSettings()
    settings.download_folder = "/tmp/downloads"
    settings.min_free_space_gb = 1
    settings.scheduled_download_delay_minutes = delay_minutes
    settings_result = MagicMock()
    settings_result.scalar_one_or_none.return_value = settings

    results = iter((schedules_result, settings_result))
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=lambda _stmt: next(results))
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    @asynccontextmanager
    async def context():
        yield session

    return context


class ScheduledDownloadDelayTests(unittest.IsolatedAsyncioTestCase):
    async def _run_tick(self, schedule, delay_minutes):
        download = MagicMock(id=99)
        build_download = AsyncMock(return_value=download)
        download_manager = MagicMock()
        download_manager.queue_download = AsyncMock(return_value=download)
        download_manager.enqueue_persisted = AsyncMock()

        manager = ScheduledManager()
        with (
            patch(
                "services.scheduled_manager.async_session_maker",
                _session_maker(schedule, delay_minutes),
            ),
            patch(
                "services.scheduled_manager.build_download_from_program",
                build_download,
            ),
            patch("services.scheduled_manager.download_manager", download_manager),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(
                manager,
                "_get_catchup_window_days",
                new=AsyncMock(return_value=7),
            ),
            patch(
                "services.scheduled_manager.epg_service.fill_gaps_from_stored",
                new=AsyncMock(),
            ),
        ):
            await manager._queue_ready_recordings()

        return build_download

    async def test_schedule_waits_until_configured_delay_has_elapsed(self):
        schedule = _make_schedule(minutes_since_end=2)

        build_download = await self._run_tick(schedule, delay_minutes=5)

        build_download.assert_not_awaited()
        self.assertEqual(schedule.status, ScheduledStatus.SCHEDULED.value)

    async def test_zero_delay_dispatches_as_soon_as_recording_has_ended(self):
        schedule = _make_schedule(minutes_since_end=2)

        build_download = await self._run_tick(schedule, delay_minutes=0)

        build_download.assert_awaited_once()
        self.assertEqual(schedule.status, ScheduledStatus.QUEUED.value)

    def test_api_available_at_includes_padding_then_download_delay(self):
        end = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        schedule = ScheduledRecording(
            account_id=1,
            channel_id="101",
            channel_name="Test Channel",
            program_title="Test Show",
            program_start=end - timedelta(hours=1),
            program_end=end,
            stop_timestamp=int(end.timestamp()),
            duration_minutes=60,
            post_padding_minutes=10,
        )

        payload = schedule.to_dict(download_delay_minutes=5)

        self.assertEqual(payload["available_at"], "2026-09-03T12:15:00+00:00")

    async def test_migration_adds_delay_with_five_minute_default(self):
        conn = AsyncMock()

        async def column_exists(_conn, table_name, column_name):
            return not (
                table_name == "app_settings"
                and column_name == "scheduled_download_delay_minutes"
            )

        with patch("database._column_exists", side_effect=column_exists):
            await _apply_lightweight_migrations(conn)

        executed = [str(call.args[0]) for call in conn.execute.await_args_list]
        self.assertIn(
            "ALTER TABLE app_settings ADD COLUMN "
            "scheduled_download_delay_minutes INTEGER DEFAULT 5",
            executed,
        )


if __name__ == "__main__":
    unittest.main()
