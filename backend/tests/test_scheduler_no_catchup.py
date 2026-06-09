"""
Regression tests: scheduled_manager handles channels that don't support catchup.

Two cases from the bug report:

1. Provider says tv_archive=0 (no catchup): schedule must be marked FAILED with a
   plain-English message instead of being dispatched with a phantom 30-day window.

2. Provider API call fails with a network error: schedule must stay SCHEDULED so
   the manager retries on the next 30-second poll, not dispatched blindly.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, ScheduledRecording, ScheduledStatus
from services.epg_service import NoCatchupSupportError
from services.scheduled_manager import ScheduledManager


def _make_ready_schedule() -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        id=10,
        account_id=1,
        channel_id="200",
        channel_name="No Archive Channel",
        program_title="Live News",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedule):
    scalars_result = MagicMock()
    scalars_result.all.return_value = [schedule]
    schedules_exec_result = MagicMock()
    schedules_exec_result.scalars.return_value = scalars_result

    settings_obj = AppSettings()
    settings_obj.download_folder = "/tmp/downloads"
    settings_obj.min_free_space_gb = 1
    settings_exec_result = MagicMock()
    settings_exec_result.scalar_one_or_none.return_value = settings_obj

    call_count = [0]

    async def execute(stmt):
        call_count[0] += 1
        if call_count[0] == 1:
            return schedules_exec_result
        return settings_exec_result

    session = AsyncMock()
    session.execute = execute
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx, session


class NoCatchupScheduleTests(unittest.IsolatedAsyncioTestCase):
    """_queue_ready_recordings must handle non-catchup channels without dispatching."""

    async def test_no_catchup_channel_marked_failed(self):
        """Schedule for a channel with tv_archive=0 must be marked FAILED with a
        clear message, not dispatched as a 30-day-window download."""
        schedule = _make_ready_schedule()
        session_maker, session = _make_session_maker(schedule)
        manager = ScheduledManager()

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock()

        async def raise_no_catchup(sess, schedule):
            raise NoCatchupSupportError(
                "'No Archive Channel' does not support catchup recording."
            )

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=raise_no_catchup),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            schedule.status,
            ScheduledStatus.FAILED.value,
            "Schedule on a non-catchup channel must be marked FAILED, not dispatched.",
        )
        self.assertIsNotNone(schedule.status_message)
        self.assertIn(
            "catchup",
            schedule.status_message.lower(),
            "status_message must explain catchup is not supported.",
        )
        fake_dm.queue_download.assert_not_called()

    async def test_provider_api_error_leaves_schedule_in_scheduled(self):
        """When get_channel_archive_days raises a network error, the schedule must
        stay in SCHEDULED state so it can be retried on the next poll."""
        schedule = _make_ready_schedule()
        session_maker, session = _make_session_maker(schedule)
        manager = ScheduledManager()

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock()

        async def raise_network_error(sess, schedule):
            raise ConnectionError("Provider unreachable")

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=raise_network_error),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            schedule.status,
            ScheduledStatus.SCHEDULED.value,
            "Schedule must stay SCHEDULED when the provider API is unreachable.",
        )
        fake_dm.queue_download.assert_not_called()

    async def test_no_catchup_status_committed(self):
        """FAILED status for a non-catchup schedule must be committed to the DB."""
        schedule = _make_ready_schedule()
        session_maker, session = _make_session_maker(schedule)
        manager = ScheduledManager()

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock()

        async def raise_no_catchup(sess, schedule):
            raise NoCatchupSupportError("Channel does not support catchup recording.")

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=raise_no_catchup),
        ):
            await manager._queue_ready_recordings()

        session.commit.assert_called()


class GetChannelArchiveDaysTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for EPGService.get_channel_archive_days."""

    async def test_raises_when_channel_not_found(self):
        """NoCatchupSupportError raised when channel_id absent from provider list."""
        from services.epg_service import EPGService

        service = EPGService()

        async def fake_get_live_streams():
            return [{"stream_id": 999, "tv_archive": 1, "tv_archive_duration": 7}]

        mock_client = MagicMock()
        mock_client.get_live_streams = AsyncMock(side_effect=fake_get_live_streams)
        mock_client.close = AsyncMock()

        with patch.object(service, "_get_client", return_value=mock_client):
            with self.assertRaises(NoCatchupSupportError):
                await service.get_channel_archive_days(MagicMock(), account_id=1, channel_id="404")

    async def test_raises_when_tv_archive_zero(self):
        """NoCatchupSupportError raised when provider sets tv_archive=0."""
        from services.epg_service import EPGService

        service = EPGService()

        async def fake_get_live_streams():
            return [{"stream_id": 1, "name": "News HD", "tv_archive": 0}]

        mock_client = MagicMock()
        mock_client.get_live_streams = AsyncMock(side_effect=fake_get_live_streams)
        mock_client.close = AsyncMock()

        with patch.object(service, "_get_client", return_value=mock_client):
            with self.assertRaises(NoCatchupSupportError):
                await service.get_channel_archive_days(MagicMock(), account_id=1, channel_id="1")

    async def test_returns_days_when_catchup_supported(self):
        """Returns tv_archive_duration as int when provider sets tv_archive=1."""
        from services.epg_service import EPGService

        service = EPGService()

        async def fake_get_live_streams():
            return [{"stream_id": 1, "name": "Sports HD", "tv_archive": 1, "tv_archive_duration": 14}]

        mock_client = MagicMock()
        mock_client.get_live_streams = AsyncMock(side_effect=fake_get_live_streams)
        mock_client.close = AsyncMock()

        with patch.object(service, "_get_client", return_value=mock_client):
            days = await service.get_channel_archive_days(MagicMock(), account_id=1, channel_id="1")

        self.assertEqual(days, 14)


if __name__ == "__main__":
    unittest.main()
