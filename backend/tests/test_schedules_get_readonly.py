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


if __name__ == "__main__":
    unittest.main()
