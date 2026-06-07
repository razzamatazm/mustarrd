import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import ScheduledRecording, ScheduledStatus, DownloadStatus
from services.download_manager import DownloadManager


def _make_schedule(download_id=1, status=ScheduledStatus.QUEUED.value):
    s = MagicMock(spec=ScheduledRecording)
    s.download_id = download_id
    s.status = status
    s.status_message = None
    s.updated_at = None
    return s


class SyncScheduleStatusTests(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests for GET /schedules side-effect bug.

    Before the fix, list_schedules wrote schedule.status to the DB on every
    GET request whenever the linked download had moved to a different state.
    A GET during a transient failure window permanently stuck the schedule as
    FAILED even after the download recovered.

    Fix: remove the DB write from list_schedules; move status sync into
    download_manager._sync_schedule_status, called at each terminal transition.
    """

    async def _make_session(self, schedule=None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = schedule
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_sync_schedule_status_updates_linked_record(self):
        """_sync_schedule_status must update schedule.status when it differs."""
        schedule = _make_schedule(download_id=42, status=ScheduledStatus.QUEUED.value)
        session = await self._make_session(schedule)

        manager = DownloadManager()
        await manager._sync_schedule_status(session, 42, DownloadStatus.COMPLETED.value)

        self.assertEqual(
            schedule.status,
            DownloadStatus.COMPLETED.value,
            "_sync_schedule_status must set schedule.status to the new terminal status.",
        )
        self.assertIsNone(
            schedule.status_message,
            "_sync_schedule_status must clear status_message.",
        )

    async def test_sync_schedule_status_noop_when_already_matches(self):
        """_sync_schedule_status must not mutate schedule when status already matches."""
        schedule = _make_schedule(status=ScheduledStatus.COMPLETED.value)
        original_updated_at = schedule.updated_at
        session = await self._make_session(schedule)

        manager = DownloadManager()
        await manager._sync_schedule_status(session, 1, DownloadStatus.COMPLETED.value)

        self.assertEqual(schedule.updated_at, original_updated_at,
                         "updated_at must not change when status already matches.")

    async def test_sync_schedule_status_noop_when_no_linked_schedule(self):
        """_sync_schedule_status must silently do nothing when no schedule links this download."""
        session = await self._make_session(schedule=None)

        manager = DownloadManager()
        # Must not raise
        await manager._sync_schedule_status(session, 999, DownloadStatus.FAILED.value)

    async def test_list_schedules_no_db_commit(self):
        """
        GET /schedules must not commit to the DB even when a linked download has
        a different status than the schedule's stored status.

        Before the fix, list_schedules would commit whenever any schedule's
        download.status differed from schedule.status. This could permanently
        overwrite a schedule as FAILED during a transient download failure.
        """
        from api.schedules import list_schedules

        schedule = _make_schedule(download_id=7, status=ScheduledStatus.QUEUED.value)
        schedule.to_dict.return_value = {
            "id": 1,
            "status": ScheduledStatus.QUEUED.value,
            "download_id": 7,
        }

        download = MagicMock()
        download.id = 7
        download.status = "failed"  # transient failure
        download.progress = 0.0
        download.output_path = "/tmp/test.ts"

        schedules_result = MagicMock()
        schedules_result.scalars.return_value.all.return_value = [schedule]

        downloads_result = MagicMock()
        downloads_result.scalars.return_value.all.return_value = [download]

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return schedules_result
            return downloads_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        auth = MagicMock()
        auth.is_admin = True
        auth.user_id = 1

        response = await list_schedules(auth=auth, session=session)

        session.commit.assert_not_called()

        self.assertEqual(len(response), 1)
        self.assertEqual(
            response[0]["status"],
            "failed",
            "Response must reflect the download's live status for display.",
        )


class CancelDownloadSyncTests(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests for cancel_download not syncing linked ScheduledRecording.

    Before the fix, cancel_download() committed DownloadStatus.CANCELLED to the
    Download row but never called _sync_schedule_status, leaving the linked
    ScheduledRecording.status as 'queued' or 'downloading'. create_schedule() then
    treated that stale row as still active in its duplicate check, blocking any
    re-schedule after a user cancel.
    """

    async def test_cancel_download_syncs_schedule_status(self):
        """cancel_download must update linked ScheduledRecording to CANCELLED."""
        download = MagicMock()
        download.id = 5
        download.status = DownloadStatus.PENDING.value

        schedule = _make_schedule(download_id=5, status=ScheduledStatus.QUEUED.value)

        download_result = MagicMock()
        download_result.scalar_one_or_none.return_value = download

        schedule_result = MagicMock()
        schedule_result.scalar_one_or_none.return_value = schedule

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return download_result
            return schedule_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_session_maker():
            yield session

        manager = DownloadManager()

        with patch("services.download_manager.async_session_maker", mock_session_maker):
            result = await manager.cancel_download(5)

        self.assertTrue(result)
        self.assertEqual(download.status, DownloadStatus.CANCELLED.value)
        self.assertEqual(
            schedule.status,
            DownloadStatus.CANCELLED.value,
            "cancel_download must sync linked ScheduledRecording.status to CANCELLED.",
        )
        session.commit.assert_called_once()

    async def test_cancel_download_no_linked_schedule_still_commits(self):
        """cancel_download must still cancel the download when no schedule is linked."""
        download = MagicMock()
        download.id = 9
        download.status = DownloadStatus.PENDING.value

        download_result = MagicMock()
        download_result.scalar_one_or_none.return_value = download

        schedule_result = MagicMock()
        schedule_result.scalar_one_or_none.return_value = None

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return download_result
            return schedule_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        @asynccontextmanager
        async def mock_session_maker():
            yield session

        manager = DownloadManager()

        with patch("services.download_manager.async_session_maker", mock_session_maker):
            result = await manager.cancel_download(9)

        self.assertTrue(result)
        self.assertEqual(download.status, DownloadStatus.CANCELLED.value)
        session.commit.assert_called_once()


class RecoveryScheduleSyncTests(unittest.IsolatedAsyncioTestCase):
    """
    Regression tests: recover_incomplete_downloads must sync linked ScheduledRecording.

    Before the fix, restart-recovery set terminal Download.status (COMPLETED/FAILED)
    without calling _sync_schedule_status, leaving the ScheduledRecording at
    'queued'/'downloading'. create_schedule() then blocked re-scheduling with a 409.
    """

    async def _make_recovery_session(self, downloads, settings=None):
        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = settings

        downloads_result = MagicMock()
        downloads_result.scalars.return_value.all.return_value = downloads

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return settings_result
            return downloads_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()
        return session

    async def test_recovery_failed_syncs_schedule_to_failed(self):
        """PROCESSING download with no recoverable file syncs schedule to FAILED on restart-recovery."""
        download = MagicMock()
        download.id = 20
        download.status = DownloadStatus.PROCESSING.value
        download.output_path = "/tmp/nonexistent_recovery_test_99999.ts"
        download.is_vod = True  # skip _needs_post_processing branch

        session = await self._make_recovery_session([download])

        @asynccontextmanager
        async def mock_session_maker():
            yield session

        manager = DownloadManager()
        sync_calls = []

        async def spy_sync(sess, dl_id, status):
            sync_calls.append((dl_id, status))

        manager._sync_schedule_status = spy_sync

        with (
            patch("services.download_manager.async_session_maker", mock_session_maker),
            patch.object(manager, "_broadcast_log", AsyncMock()),
            patch.object(manager, "_broadcast_progress", AsyncMock()),
        ):
            await manager.recover_incomplete_downloads()

        self.assertIn(
            (20, DownloadStatus.FAILED.value),
            sync_calls,
            "Recovery must call _sync_schedule_status with FAILED when input file is missing.",
        )

    async def test_recovery_completed_syncs_schedule_to_completed(self):
        """PROCESSING download already in completed folder syncs schedule to COMPLETED on restart-recovery."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name
        try:
            download = MagicMock()
            download.id = 21
            download.status = DownloadStatus.PROCESSING.value
            download.output_path = tmp_path
            download.completed_at = None
            download.error_message = None
            download.is_vod = True

            session = await self._make_recovery_session([download])

            @asynccontextmanager
            async def mock_session_maker():
                yield session

            manager = DownloadManager()
            sync_calls = []

            async def spy_sync(sess, dl_id, status):
                sync_calls.append((dl_id, status))

            manager._sync_schedule_status = spy_sync

            with (
                patch("services.download_manager.async_session_maker", mock_session_maker),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
                patch.object(manager, "_path_is_under", return_value=True),
                patch.object(manager, "_resolve_completed_folder", return_value=os.path.dirname(tmp_path)),
                patch.object(manager, "_resolve_download_folder", return_value="/tmp/downloads_test"),
            ):
                await manager.recover_incomplete_downloads()

            self.assertIn(
                (21, DownloadStatus.COMPLETED.value),
                sync_calls,
                "Recovery must call _sync_schedule_status with COMPLETED when file is in completed folder.",
            )
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
