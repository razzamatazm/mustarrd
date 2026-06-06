import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus


def _make_cancelled_download(dl_id):
    d = MagicMock()
    d.id = dl_id
    d.status = DownloadStatus.CANCELLED.value
    d.progress = 50.0
    d.downloaded_bytes = 1024
    d.error_message = None
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


class RetryCancelledFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_clears_cancelled_flag(self):
        """
        BUG: retry_download does not remove download_id from self._cancelled.

        After a user cancels a download and then retries it, the id remains in
        the _cancelled set. process_queue sees it and discards the job without
        starting it, leaving the download stuck as PENDING forever.

        This test FAILS on the current codebase because retry_download puts the
        id back on the queue but never calls self._cancelled.discard(download_id).

        Fix: add `self._cancelled.discard(download_id)` in retry_download before
        calling `await self._queue.put(download_id)`.
        """
        manager = DownloadManager()
        dl_id = 7

        # Simulate the download having been cancelled: id is in _cancelled.
        manager._cancelled.add(dl_id)

        download = _make_cancelled_download(dl_id)

        with patch("services.download_manager.async_session_maker", _make_session_maker(download)):
            result = await manager.retry_download(dl_id)

        self.assertTrue(result, "retry_download must return True for a CANCELLED download.")

        # This assertion FAILS on the current codebase.
        self.assertNotIn(
            dl_id,
            manager._cancelled,
            "retry_download must remove download_id from _cancelled. "
            "Currently it leaves the flag set, so process_queue silently discards "
            "the retried job and the download stays PENDING forever.",
        )

    async def test_retry_failed_download_also_clears_cancelled_flag(self):
        """
        Same issue applies when retrying a FAILED download that also has a stale
        cancelled flag (e.g., cancelled then failed during the cancel teardown).
        """
        manager = DownloadManager()
        dl_id = 8

        manager._cancelled.add(dl_id)

        failed = _make_cancelled_download(dl_id)
        failed.status = DownloadStatus.FAILED.value

        with patch("services.download_manager.async_session_maker", _make_session_maker(failed)):
            result = await manager.retry_download(dl_id)

        self.assertTrue(result)
        self.assertNotIn(
            dl_id,
            manager._cancelled,
            "retry_download must clear stale _cancelled flag for FAILED downloads too.",
        )


if __name__ == "__main__":
    unittest.main()
