"""
Regression test: schedule cancelled mid-dispatch must not start a download.

_queue_ready_recordings holds no DB lock between its initial SELECT and the
final commit. If the user cancels the schedule while we await
build_download_from_program or queue_download, the cancel's commit sets
status=CANCELLED in the DB. Without the fix, the manager then overwrites that
with status=QUEUED. With the fix it re-reads the status via session.refresh()
and cancels the queued download instead.
"""
import asyncio
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
from services.scheduled_manager import ScheduledManager


def _make_ready_schedule() -> ScheduledRecording:
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=5)
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
        status=ScheduledStatus.SCHEDULED.value,
    )


def _make_session_maker(schedule, on_refresh=None):
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

    async def refresh(obj):
        if on_refresh:
            on_refresh(obj)

    session = AsyncMock()
    session.execute = execute
    session.refresh = AsyncMock(side_effect=refresh)
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx, session


class CancelDuringDispatchTests(unittest.IsolatedAsyncioTestCase):
    """_queue_ready_recordings must not queue a download for a cancelled schedule."""

    async def test_cancel_during_dispatch_aborts_download(self):
        """Download must be cancelled and schedule must not be set to QUEUED
        when the user cancels the schedule while build_download_from_program is running."""
        schedule = _make_ready_schedule()
        cancelled_downloads: list = []

        async def fake_build(*args, **kwargs):
            dl = MagicMock()
            dl.id = 99
            return dl

        async def fake_queue(dl):
            return dl

        async def fake_cancel(download_id):
            cancelled_downloads.append(download_id)
            return True

        # Simulate the DB returning CANCELLED when session.refresh() is called.
        def on_refresh(obj):
            obj.status = ScheduledStatus.CANCELLED.value

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=fake_queue)
        fake_dm.cancel_download = AsyncMock(side_effect=fake_cancel)

        session_maker, session = _make_session_maker(schedule, on_refresh=on_refresh)
        manager = ScheduledManager()

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            cancelled_downloads,
            [99],
            "cancel_download must be called with the queued download's ID when user cancels mid-dispatch.",
        )
        self.assertNotEqual(
            schedule.status,
            ScheduledStatus.QUEUED.value,
            "Schedule must not be set to QUEUED after user cancels mid-dispatch.",
        )

    async def test_no_cancel_proceeds_normally(self):
        """When no cancel happens during dispatch, the schedule should be set to QUEUED."""
        schedule = _make_ready_schedule()

        async def fake_build(*args, **kwargs):
            dl = MagicMock()
            dl.id = 77
            return dl

        async def fake_queue(dl):
            return dl

        # on_refresh does nothing: status stays SCHEDULED (no cancel happened)
        session_maker, session = _make_session_maker(schedule)
        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=fake_queue)
        fake_dm.cancel_download = AsyncMock()

        manager = ScheduledManager()

        with (
            patch("services.scheduled_manager.async_session_maker", session_maker),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

        self.assertEqual(
            schedule.status,
            ScheduledStatus.QUEUED.value,
            "Normal (no-cancel) dispatch must set schedule to QUEUED.",
        )
        fake_dm.cancel_download.assert_not_called()


    async def test_commit_fires_between_schedules(self):
        """Per-schedule commit: session.commit() must fire after schedule A is set to
        QUEUED and before schedule B's build starts.

        Without this, a cancel of A during B's build sees download_id=NULL in the DB
        (A not yet committed) and cannot cancel the download. The download then runs
        after the final end-of-loop commit overwrites the CANCELLED status with QUEUED.
        """
        schedule_a = _make_ready_schedule()
        schedule_a.id = 1
        schedule_b = _make_ready_schedule()
        schedule_b.id = 2

        events = []
        build_call = [0]

        async def fake_build(*args, **kwargs):
            build_call[0] += 1
            dl = MagicMock()
            dl.id = build_call[0] * 10
            events.append(f"build_{build_call[0]}")
            return dl

        async def fake_queue(dl):
            return dl

        async def fake_commit():
            events.append("commit")

        scalars_result = MagicMock()
        scalars_result.all.return_value = [schedule_a, schedule_b]
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
        session.refresh = AsyncMock()
        session.commit = AsyncMock(side_effect=fake_commit)

        @asynccontextmanager
        async def ctx():
            yield session

        fake_dm = MagicMock()
        fake_dm.queue_download = AsyncMock(side_effect=fake_queue)
        fake_dm.cancel_download = AsyncMock()

        manager = ScheduledManager()

        with (
            patch("services.scheduled_manager.async_session_maker", ctx),
            patch("services.scheduled_manager.build_download_from_program", new=fake_build),
            patch("services.scheduled_manager.download_manager", fake_dm),
            patch.object(manager, "_get_free_space_gb", return_value=100.0),
            patch.object(manager, "_get_catchup_window_days", new=AsyncMock(return_value=7)),
        ):
            await manager._queue_ready_recordings()

        build_1_idx = events.index("build_1")
        build_2_idx = events.index("build_2")
        commit_between = any(
            build_1_idx < i < build_2_idx
            for i, e in enumerate(events)
            if e == "commit"
        )
        self.assertTrue(
            commit_between,
            f"A commit must fire between dispatching schedule A and schedule B. "
            f"Got event order: {events}",
        )


if __name__ == "__main__":
    unittest.main()
