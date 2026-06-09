"""
Regression test: retry_download() revives a CANCELLED ScheduledRecording to QUEUED.

Before the fix:
- When a user cancels a schedule (schedule.status = CANCELLED) and then retries the
  linked CANCELLED download from the Downloads page, retry_download() calls
  _sync_schedule_status(..., ScheduledStatus.QUEUED.value) unconditionally.
- _sync_schedule_status sees CANCELLED != QUEUED and overwrites to QUEUED, silently
  reviving the user-cancelled schedule.
- Secondary harm: the revived QUEUED schedule is in active_statuses, so a subsequent
  POST /schedules for the same program returns 409 "This program is already scheduled"
  even though the user cancelled it.

After the fix:
- _sync_schedule_status (or retry_download before calling it) must skip the status
  update when schedule.status is already CANCELLED or COMPLETED. User-set terminal
  states must not be overwritten by a download retry.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import DownloadStatus, ScheduledStatus
from services.download_manager import DownloadManager


def _make_fake_session(download_status, schedule_status):
    """
    Return a fake async_session_maker whose session returns a Download on the
    first execute call and a ScheduledRecording on the second execute call.

    retry_download() opens one session and calls session.execute twice:
      1. select(Download)             -- returns the download mock
      2. select(ScheduledRecording)   -- via _sync_schedule_status, returns schedule mock
    """
    download = MagicMock()
    download.id = 42
    download.status = download_status
    download.progress = 50
    download.downloaded_bytes = 1000
    download.error_message = "Connection refused"
    download.completed_at = "2026-01-01T00:00:00"

    schedule = MagicMock()
    schedule.download_id = 42
    schedule.status = schedule_status
    schedule.status_message = "Cancelled by user"

    call_count = [0]

    async def fake_execute(stmt, *args, **kwargs):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:
            result.scalar_one_or_none.return_value = download
        else:
            result.scalar_one_or_none.return_value = schedule
        return result

    fake_session = AsyncMock()
    fake_session.execute = fake_execute
    fake_session.commit = AsyncMock()

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker, download, schedule


class RetryDownloadCancelledScheduleTests(unittest.IsolatedAsyncioTestCase):

    async def test_retry_does_not_revive_cancelled_schedule(self):
        """
        retry_download() must not change a CANCELLED ScheduledRecording to QUEUED.

        This test FAILS on the current codebase because _sync_schedule_status
        unconditionally sets schedule.status = QUEUED when it sees CANCELLED != QUEUED.

        Fix: skip the status update in _sync_schedule_status (or in retry_download
        before the call) when schedule.status is already CANCELLED or COMPLETED.
        """
        manager = DownloadManager()
        maker, download, schedule = _make_fake_session(
            DownloadStatus.CANCELLED.value,
            ScheduledStatus.CANCELLED.value,
        )

        with patch("services.download_manager.async_session_maker", side_effect=maker):
            result = await manager.retry_download(42)

        self.assertTrue(result, "retry_download must return True for a CANCELLED download.")
        self.assertEqual(
            download.status,
            DownloadStatus.PENDING.value,
            "retry_download must reset Download.status to PENDING.",
        )
        # This assertion FAILS on current code: _sync_schedule_status overwrites
        # CANCELLED with QUEUED, reviving the user-cancelled schedule.
        self.assertEqual(
            schedule.status,
            ScheduledStatus.CANCELLED.value,
            "retry_download must not overwrite a user-cancelled schedule with QUEUED. "
            "Current behaviour sets schedule.status = QUEUED, which revives the "
            "cancelled schedule and blocks re-scheduling via the dedup guard.",
        )

    async def test_retry_completed_schedule_not_revived(self):
        """
        retry_download() must not change a COMPLETED ScheduledRecording to QUEUED.

        A completed schedule is a terminal state set by normal operation. Retrying
        a failed download (e.g., a post-processing failure after the initial
        download completed) must not revert the schedule's completed record to QUEUED.
        """
        manager = DownloadManager()
        maker, download, schedule = _make_fake_session(
            DownloadStatus.FAILED.value,
            ScheduledStatus.COMPLETED.value,
        )

        with patch("services.download_manager.async_session_maker", side_effect=maker):
            result = await manager.retry_download(42)

        self.assertTrue(result, "retry_download must return True for a FAILED download.")
        self.assertEqual(
            schedule.status,
            ScheduledStatus.COMPLETED.value,
            "retry_download must not overwrite a COMPLETED ScheduledRecording with QUEUED.",
        )


if __name__ == "__main__":
    unittest.main()
