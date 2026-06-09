"""
Regression test: the scheduler must fetch each account's channel list at most
once per tick.

Before fix: _queue_ready_recordings called epg_service.get_channel_archive_days
for every ready schedule, and that method issued one get_live_streams() provider
API call each time. N due schedules on the same account meant N identical
provider calls in a single 30-second tick.

After fix: the channel list is fetched once per account per tick (failures are
cached too) and shared across all of that tick's catchup-window checks.
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
from services.epg_service import epg_service
from services.scheduled_manager import ScheduledManager


def _make_ready_schedule(schedule_id: int, account_id: int, channel_id: str) -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    return ScheduledRecording(
        id=schedule_id,
        account_id=account_id,
        channel_id=channel_id,
        channel_name=f"Channel {channel_id}",
        program_title=f"Show {schedule_id}",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedules):
    scalars_result = MagicMock()
    scalars_result.all.return_value = schedules
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
    session.refresh = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


def _make_fake_dm():
    fake_dm = MagicMock()
    fake_dm.queue_download = AsyncMock(side_effect=lambda dl, session=None: dl)
    fake_dm.enqueue_persisted = AsyncMock(side_effect=lambda dl: dl)
    return fake_dm


def _make_client(channels):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=channels)
    client.close = AsyncMock()
    return client


class SchedulerChannelListBatchTests(unittest.IsolatedAsyncioTestCase):
    """One get_live_streams() call per account per tick, shared across dispatches."""

    async def _run_tick(self, schedules, client_for_account):
        async def fake_build(*args, **kwargs):
            dl = MagicMock()
            dl.id = 99
            return dl

        manager = ScheduledManager()
        with (
            patch("services.scheduled_manager.async_session_maker", _make_session_maker(schedules)),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", _make_fake_dm()),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(epg_service, "_get_client", new=client_for_account),
        ):
            await manager._queue_ready_recordings()

    async def test_one_channel_list_fetch_for_many_schedules_same_account(self):
        """REGRESSION: 3 due schedules on one account must trigger exactly one
        get_live_streams() provider call, not three."""
        schedules = [
            _make_ready_schedule(1, account_id=1, channel_id="101"),
            _make_ready_schedule(2, account_id=1, channel_id="102"),
            _make_ready_schedule(3, account_id=1, channel_id="103"),
        ]
        client = _make_client([
            {"stream_id": 101, "tv_archive": 1, "tv_archive_duration": 7},
            {"stream_id": 102, "tv_archive": 1, "tv_archive_duration": 7},
            {"stream_id": 103, "tv_archive": 1, "tv_archive_duration": 7},
        ])
        get_client = AsyncMock(return_value=client)

        await self._run_tick(schedules, get_client)

        self.assertEqual(
            client.get_live_streams.await_count, 1,
            "Channel list must be fetched once per account per tick, not per schedule.",
        )
        for schedule in schedules:
            self.assertEqual(
                schedule.status, ScheduledStatus.QUEUED.value,
                f"Schedule {schedule.id} must still be dispatched from the shared list.",
            )

    async def test_one_fetch_per_account_for_two_accounts(self):
        """Schedules across two accounts trigger one fetch per account."""
        schedules = [
            _make_ready_schedule(1, account_id=1, channel_id="101"),
            _make_ready_schedule(2, account_id=2, channel_id="201"),
            _make_ready_schedule(3, account_id=1, channel_id="102"),
        ]
        clients = {
            1: _make_client([
                {"stream_id": 101, "tv_archive": 1, "tv_archive_duration": 7},
                {"stream_id": 102, "tv_archive": 1, "tv_archive_duration": 7},
            ]),
            2: _make_client([
                {"stream_id": 201, "tv_archive": 1, "tv_archive_duration": 7},
            ]),
        }

        async def get_client(session, account_id):
            return clients[account_id]

        await self._run_tick(schedules, get_client)

        self.assertEqual(clients[1].get_live_streams.await_count, 1)
        self.assertEqual(clients[2].get_live_streams.await_count, 1)
        for schedule in schedules:
            self.assertEqual(schedule.status, ScheduledStatus.QUEUED.value)

    async def test_provider_failure_fetched_once_and_schedules_retained(self):
        """A failing provider is also called only once per tick, and the
        affected schedules stay SCHEDULED for the next poll."""
        schedules = [
            _make_ready_schedule(1, account_id=1, channel_id="101"),
            _make_ready_schedule(2, account_id=1, channel_id="102"),
        ]
        client = MagicMock()
        client.get_live_streams = AsyncMock(side_effect=ConnectionError("Provider unreachable"))
        client.close = AsyncMock()
        get_client = AsyncMock(return_value=client)

        await self._run_tick(schedules, get_client)

        self.assertEqual(
            client.get_live_streams.await_count, 1,
            "A failing provider must not be retried for every schedule in the same tick.",
        )
        for schedule in schedules:
            self.assertEqual(
                schedule.status, ScheduledStatus.SCHEDULED.value,
                "Schedules must stay SCHEDULED when the provider is unreachable.",
            )


if __name__ == "__main__":
    unittest.main()
