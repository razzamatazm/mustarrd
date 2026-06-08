"""
Regression test: queue_download must refuse to queue a download when an active
download is already writing to the same output_path.

Before fix: two scheduled recordings with different start_timestamps but the same
resolved output_path (e.g. a provider shifts EPG timestamps by 1 second between
refreshes) both passed through queue_download. Both tasks opened the same .ts file
simultaneously, producing a corrupt or empty recording.

After fix: queue_download queries for active (PENDING/DOWNLOADING/PROCESSING)
downloads with the same output_path before inserting. If one is found it raises
ValueError so scheduled_manager marks the second schedule FAILED instead of spawning
a second write task.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import Download, DownloadStatus
from services.download_manager import DownloadManager


def _make_row(
    output_path="/downloads/show.ts",
    status=DownloadStatus.PENDING.value,
    start_ts=1000,
    stop_ts=2000,
):
    return Download(
        account_id=1,
        channel_id="ch1",
        channel_name="Test Channel",
        program_title="Show",
        program_start=datetime(2024, 1, 1, 20, 0),
        program_end=datetime(2024, 1, 1, 21, 0),
        start_timestamp=start_ts,
        stop_timestamp=stop_ts,
        duration_minutes=60,
        source_url="http://provider.test/ts",
        output_path=output_path,
        status=status,
    )


class QueueOutputPathDedupTests(unittest.IsolatedAsyncioTestCase):
    """queue_download raises ValueError when output_path conflicts with an active download."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.manager._queue = AsyncMock()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _session_cm(self):
        factory = self.session_factory

        @asynccontextmanager
        async def _cm():
            async with factory() as session:
                yield session

        return _cm

    async def _queue(self, download):
        with patch("services.download_manager.async_session_maker", self._session_cm()):
            with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                return await self.manager.queue_download(download)

    async def test_same_output_path_pending_raises(self):
        """Second download to same output_path blocked when first is PENDING.

        Before fix: both downloads were accepted; both opened the same .ts file
        simultaneously, corrupting the recording.
        After fix: ValueError raised; scheduled_manager marks second schedule FAILED.
        """
        async with self.session_factory() as session:
            session.add(_make_row(status=DownloadStatus.PENDING.value))
            await session.commit()

        # Simulate EPG timestamp drift: start_ts shifted by 1 second, same output_path.
        new_dl = _make_row(start_ts=1001)
        with self.assertRaises(ValueError) as ctx:
            await self._queue(new_dl)
        self.assertIn("already active", str(ctx.exception))

    async def test_same_output_path_downloading_raises(self):
        """Blocked when existing download is DOWNLOADING."""
        async with self.session_factory() as session:
            session.add(_make_row(status=DownloadStatus.DOWNLOADING.value))
            await session.commit()

        with self.assertRaises(ValueError):
            await self._queue(_make_row(start_ts=1001))

    async def test_same_output_path_processing_raises(self):
        """Blocked when existing download is PROCESSING."""
        async with self.session_factory() as session:
            session.add(_make_row(status=DownloadStatus.PROCESSING.value))
            await session.commit()

        with self.assertRaises(ValueError):
            await self._queue(_make_row(start_ts=1001))

    async def test_different_output_path_allowed(self):
        """Different output_path: no conflict, download proceeds normally."""
        async with self.session_factory() as session:
            session.add(_make_row(output_path="/downloads/show.ts", status=DownloadStatus.PENDING.value))
            await session.commit()

        new_dl = _make_row(output_path="/downloads/other-show.ts", start_ts=3000, stop_ts=4000)
        result = await self._queue(new_dl)
        self.assertIsNotNone(result.id)

    async def test_completed_same_path_allowed(self):
        """COMPLETED is terminal: a new download to the same output_path is allowed."""
        async with self.session_factory() as session:
            session.add(_make_row(status=DownloadStatus.COMPLETED.value))
            await session.commit()

        new_dl = _make_row(start_ts=3000, stop_ts=4000)
        result = await self._queue(new_dl)
        self.assertIsNotNone(result.id)

    async def test_failed_same_path_allowed(self):
        """FAILED is terminal: a retry download to the same output_path is allowed."""
        async with self.session_factory() as session:
            session.add(_make_row(status=DownloadStatus.FAILED.value))
            await session.commit()

        new_dl = _make_row(start_ts=3000, stop_ts=4000)
        result = await self._queue(new_dl)
        self.assertIsNotNone(result.id)

    async def test_no_existing_downloads_allowed(self):
        """No active downloads: queuing proceeds normally."""
        new_dl = _make_row()
        result = await self._queue(new_dl)
        self.assertIsNotNone(result.id)


if __name__ == "__main__":
    unittest.main()
