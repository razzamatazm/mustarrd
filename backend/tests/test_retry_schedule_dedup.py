import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus, ScheduledStatus


def _make_download(dl_id, status):
    d = MagicMock()
    d.id = dl_id
    d.status = status
    d.progress = 0.0
    d.downloaded_bytes = 0
    d.error_message = "provider timeout"
    d.completed_at = None
    return d


def _make_session_maker(download):
    result = MagicMock()
    result.scalar_one_or_none.return_value = download
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


class RetryScheduleDedupTests(unittest.IsolatedAsyncioTestCase):
    """
    BUG: retry_download does not call _sync_schedule_status.

    After a user cancels or fails a scheduled download and then retries it,
    the linked ScheduledRecording.status stays "cancelled" or "failed".
    create_schedule() dedup guard checks ScheduledRecording.status IN
    (scheduled, paused_low_space, queued, downloading, processing).
    Neither "cancelled" nor "failed" is in that list, so the in-progress
    retried download is invisible to the guard.

    Result: a second POST /api/schedules for the same show succeeds, the
    scheduler fires a second download, and both writes go to the same
    output_path, corrupting the file.

    These tests FAIL on the current codebase because retry_download never
    calls _sync_schedule_status.

    Fix: call _sync_schedule_status(session, download_id, ScheduledStatus.QUEUED.value)
    inside retry_download after resetting download.status to PENDING.
    """

    @unittest.expectedFailure
    async def test_retry_cancelled_download_resets_schedule_status(self):
        """
        retry_download on a CANCELLED download must reset the linked
        ScheduledRecording status so the dedup guard blocks re-scheduling.
        """
        manager = DownloadManager()
        dl_id = 42

        download = _make_download(dl_id, DownloadStatus.CANCELLED.value)

        sync_calls = []

        async def spy_sync(session, download_id, new_status):
            sync_calls.append((download_id, new_status))

        manager._sync_schedule_status = spy_sync

        with patch("services.download_manager.async_session_maker", _make_session_maker(download)):
            result = await manager.retry_download(dl_id)

        self.assertTrue(result, "retry_download must return True for a CANCELLED download.")

        # This assertion FAILS on the current codebase because retry_download
        # never calls _sync_schedule_status.
        self.assertTrue(
            sync_calls,
            "retry_download must call _sync_schedule_status to reset the linked "
            "ScheduledRecording.status. Currently it does not, leaving the schedule "
            "as 'cancelled', which lets the dedup guard in create_schedule() be "
            "bypassed. A second POST /api/schedules for the same show then succeeds "
            "and a duplicate download is created.",
        )

        called_download_id, called_status = sync_calls[0]
        self.assertEqual(called_download_id, dl_id)
        self.assertEqual(
            called_status,
            ScheduledStatus.QUEUED.value,
            "retry_download must reset ScheduledRecording to an active status "
            "(queued) so the dedup guard recognizes it as in-flight.",
        )

    @unittest.expectedFailure
    async def test_retry_failed_download_resets_schedule_status(self):
        """
        retry_download on a FAILED download must also reset the linked
        ScheduledRecording status. Failure path is the more common real-world
        trigger: provider timeout or HTTP error marks the download failed, user
        retries, then tries to re-schedule thinking it is unscheduled.
        """
        manager = DownloadManager()
        dl_id = 43

        download = _make_download(dl_id, DownloadStatus.FAILED.value)

        sync_calls = []

        async def spy_sync(session, download_id, new_status):
            sync_calls.append((download_id, new_status))

        manager._sync_schedule_status = spy_sync

        with patch("services.download_manager.async_session_maker", _make_session_maker(download)):
            result = await manager.retry_download(dl_id)

        self.assertTrue(result, "retry_download must return True for a FAILED download.")

        self.assertTrue(
            sync_calls,
            "retry_download must call _sync_schedule_status for FAILED downloads too. "
            "Without this, the same dedup bypass applies: re-scheduling a failed-then-retried "
            "show creates a duplicate download to the same output_path.",
        )

        called_download_id, called_status = sync_calls[0]
        self.assertEqual(called_download_id, dl_id)
        self.assertEqual(
            called_status,
            ScheduledStatus.QUEUED.value,
            "retry_download must reset ScheduledRecording to QUEUED so the dedup guard "
            "recognizes it as in-flight.",
        )


if __name__ == "__main__":
    unittest.main()
