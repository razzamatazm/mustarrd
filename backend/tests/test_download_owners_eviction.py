"""Regression test: _download_owners cache must be evicted on terminal status.

_broadcast_progress already pops _stage_progress when a download reaches
COMPLETED, FAILED, or CANCELLED. It did not pop _download_owners, so that
dict grew without bound in long-running instances.

Expected: _download_owners entry is removed when broadcast receives a terminal
status (COMPLETED, FAILED, or CANCELLED).
Actual (before fix): entry persists indefinitely.
"""
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


def _make_session_maker_with_owner(owner_id):
    result = MagicMock()
    result.scalar_one_or_none.return_value = owner_id
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


class DownloadOwnersEvictionTests(unittest.IsolatedAsyncioTestCase):
    """_download_owners must be cleared when a download reaches a terminal state."""

    async def _broadcast_terminal(self, manager, dl_id, status):
        """Helper: broadcast a terminal status with no WS connections."""
        session_maker = _make_session_maker_with_owner(None)
        with patch("services.download_manager.async_session_maker", session_maker):
            await manager._broadcast_progress(dl_id, 100.0, status)

    async def test_owners_cleared_on_completed(self):
        """
        _download_owners entry must be removed when status reaches COMPLETED.

        This test FAILS before the fix because _broadcast_progress pops
        _stage_progress but not _download_owners on terminal status.
        """
        manager = DownloadManager()
        dl_id = 1
        manager._download_owners[dl_id] = 42

        await self._broadcast_terminal(manager, dl_id, DownloadStatus.COMPLETED.value)

        self.assertNotIn(
            dl_id,
            manager._download_owners,
            "_download_owners must be evicted when status reaches COMPLETED.",
        )

    async def test_owners_cleared_on_failed(self):
        manager = DownloadManager()
        dl_id = 2
        manager._download_owners[dl_id] = 99

        await self._broadcast_terminal(manager, dl_id, DownloadStatus.FAILED.value)

        self.assertNotIn(
            dl_id,
            manager._download_owners,
            "_download_owners must be evicted when status reaches FAILED.",
        )

    async def test_owners_cleared_on_cancelled(self):
        manager = DownloadManager()
        dl_id = 3
        manager._download_owners[dl_id] = 7

        await self._broadcast_terminal(manager, dl_id, DownloadStatus.CANCELLED.value)

        self.assertNotIn(
            dl_id,
            manager._download_owners,
            "_download_owners must be evicted when status reaches CANCELLED.",
        )

    async def test_owners_kept_on_non_terminal(self):
        """In-progress downloads must keep their owner entry."""
        manager = DownloadManager()
        dl_id = 4
        manager._download_owners[dl_id] = 5

        session_maker = _make_session_maker_with_owner(5)
        with patch("services.download_manager.async_session_maker", session_maker):
            await manager._broadcast_progress(dl_id, 50.0, DownloadStatus.DOWNLOADING.value)

        self.assertIn(
            dl_id,
            manager._download_owners,
            "_download_owners must be kept for non-terminal statuses.",
        )


if __name__ == "__main__":
    unittest.main()
