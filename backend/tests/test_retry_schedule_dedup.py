"""
Regression test: retry_download leaves ScheduledRecording.status stale, bypassing dedup.

Before the fix:
- retry_download() reset Download.status to PENDING and re-queued the download, but
  never called _sync_schedule_status. The linked ScheduledRecording stayed
  failed/cancelled, which is not in create_schedule()'s active_statuses list.
- A second POST /api/schedules for the same program returned 200 (no 409 conflict),
  created a duplicate ScheduledRecording, and eventually launched a second download
  writing to the same output_path, corrupting the file.

After the fix:
- retry_download() calls _sync_schedule_status(..., ScheduledStatus.QUEUED.value)
  before committing, so the linked ScheduledRecording enters the active_statuses list
  and the dedup guard blocks any duplicate schedule creation.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import DownloadStatus, ScheduledStatus
from services.download_manager import DownloadManager


def _make_fake_session(download_status):
    """Return a fake async_session_maker yielding a download with the given status."""

    def make_download(status):
        d = MagicMock()
        d.id = 42
        d.status = status
        d.progress = 50
        d.downloaded_bytes = 1000
        d.error_message = "some error"
        d.completed_at = "2026-01-01T00:00:00"
        return d

    fake_session = AsyncMock()

    async def fake_execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = make_download(download_status)
        result.scalars.return_value.all.return_value = []
        return result

    fake_session.execute = fake_execute
    fake_session.commit = AsyncMock()

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker, fake_session


class RetryDownloadScheduleDedupTests(unittest.IsolatedAsyncioTestCase):

    async def _run_retry_and_capture_sync_calls(self, download_status):
        manager = DownloadManager()
        sync_calls = []

        async def spy_sync(session, download_id, new_status):
            sync_calls.append((download_id, new_status))

        maker, _ = _make_fake_session(download_status)

        with patch("services.download_manager.async_session_maker", side_effect=maker):
            manager._sync_schedule_status = spy_sync
            result = await manager.retry_download(42)

        return result, sync_calls

    async def test_retry_cancelled_download_syncs_schedule_to_queued(self):
        """retry_download must call _sync_schedule_status(QUEUED) for CANCELLED downloads."""
        result, sync_calls = await self._run_retry_and_capture_sync_calls(
            DownloadStatus.CANCELLED.value
        )
        self.assertTrue(result, "retry_download must return True for CANCELLED downloads.")
        self.assertTrue(
            sync_calls,
            "retry_download must call _sync_schedule_status for CANCELLED downloads.",
        )
        _, called_status = sync_calls[0]
        self.assertEqual(
            called_status,
            ScheduledStatus.QUEUED.value,
            f"Expected schedule synced to QUEUED, got {called_status!r}.",
        )

    async def test_retry_failed_download_syncs_schedule_to_queued(self):
        """retry_download must call _sync_schedule_status(QUEUED) for FAILED downloads."""
        result, sync_calls = await self._run_retry_and_capture_sync_calls(
            DownloadStatus.FAILED.value
        )
        self.assertTrue(result, "retry_download must return True for FAILED downloads.")
        self.assertTrue(
            sync_calls,
            "retry_download must call _sync_schedule_status for FAILED downloads.",
        )
        _, called_status = sync_calls[0]
        self.assertEqual(
            called_status,
            ScheduledStatus.QUEUED.value,
            f"Expected schedule synced to QUEUED, got {called_status!r}.",
        )


if __name__ == "__main__":
    unittest.main()
