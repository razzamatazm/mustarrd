"""
Tests for the auto-retry sweep for failed catchup downloads.

The scheduled manager's `_auto_retry_failed_downloads` sweep re-queues FAILED
catchup downloads (via download_manager.retry_download) when:
  - the auto_retry_failed_downloads app setting is enabled (default off),
  - the download has fewer than AUTO_RETRY_MAX_ATTEMPTS automatic retries,
  - at least AUTO_RETRY_MIN_INTERVAL_MINUTES passed since the last attempt,
  - the program is still inside the channel's archive (catchup) window
    (unknown windows are treated conservatively: no retry),
  - the linked schedule was not CANCELLED or COMPLETED by the user (#330).
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    ScheduledRecording,
    ScheduledStatus,
    XtreamAccount,
)
from services.download_manager import DownloadManager
from services.epg_service import NoCatchupSupportError
from services.scheduled_manager import AUTO_RETRY_MAX_ATTEMPTS, ScheduledManager


def _failed_download(**overrides):
    now = datetime.now(timezone.utc)
    end = now - timedelta(hours=1)
    start = end - timedelta(hours=1)
    defaults = dict(
        account_id=1,
        channel_id="ch1",
        channel_name="Test Channel",
        program_title="Failed Show",
        program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None),
        start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()),
        duration_minutes=60,
        source_url="http://provider.test/stream.ts",
        output_path="/tmp/test/Failed Show.ts",
        status=DownloadStatus.FAILED.value,
        error_message="HTTP 502",
        completed_at=(now - timedelta(minutes=30)).replace(tzinfo=None),
        retry_count=0,
        is_vod=False,
    )
    defaults.update(overrides)
    return Download(**defaults)


class AutoRetryFailedDownloadsTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                id=1, name="acc", server_url="http://provider.test",
                username="u", password="p",
            ))
            settings = AppSettings(
                download_folder="/tmp/test-downloads",
                completed_folder="/tmp/test-completed",
                min_free_space_gb=1,
                auto_retry_failed_downloads=True,
            )
            session.add(settings)
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _add(self, *rows):
        async with self.session_factory() as session:
            for row in rows:
                session.add(row)
            await session.commit()
            for row in rows:
                await session.refresh(row)
        return rows

    async def _set_flag(self, enabled):
        async with self.session_factory() as session:
            result = await session.execute(select(AppSettings))
            settings = result.scalar_one()
            settings.auto_retry_failed_downloads = enabled
            await session.commit()

    async def _run_sweep(self, archive_days=7, archive_side_effect=None):
        manager = ScheduledManager()
        dm = DownloadManager()
        if archive_side_effect is not None:
            archive_mock = AsyncMock(side_effect=archive_side_effect)
        else:
            archive_mock = AsyncMock(return_value=archive_days)
        with (
            patch("services.scheduled_manager.async_session_maker", self.session_factory),
            patch("services.download_manager.async_session_maker", self.session_factory),
            patch("services.scheduled_manager.download_manager", dm),
            patch.object(manager, "_get_free_space_gb", return_value=1000.0),
            patch.object(manager, "_get_archive_days_raw", new=archive_mock),
        ):
            await manager._auto_retry_failed_downloads()
        return dm, archive_mock

    async def _get_download(self, download_id):
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            return result.scalar_one()

    async def _get_schedule(self, schedule_id):
        async with self.session_factory() as session:
            result = await session.execute(
                select(ScheduledRecording).where(ScheduledRecording.id == schedule_id)
            )
            return result.scalar_one()

    async def test_eligible_failed_download_retried(self):
        """A FAILED catchup download inside the archive window is re-queued
        and its retry bookkeeping is updated."""
        (download,) = await self._add(_failed_download())

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.PENDING.value)
        self.assertEqual(refreshed.retry_count, 1)
        self.assertIsNotNone(refreshed.last_retry_at)
        self.assertEqual(dm._queue.qsize(), 1, "Retry must enqueue the download.")

    async def test_retry_count_capped(self):
        """Once AUTO_RETRY_MAX_ATTEMPTS automatic retries are spent, the
        download stays FAILED."""
        now = datetime.now(timezone.utc)
        (download,) = await self._add(_failed_download(
            retry_count=AUTO_RETRY_MAX_ATTEMPTS,
            last_retry_at=(now - timedelta(hours=1)).replace(tzinfo=None),
        ))

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.retry_count, AUTO_RETRY_MAX_ATTEMPTS)
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_outside_archive_window_not_retried(self):
        """A program past program_end + archive window must not be retried."""
        now = datetime.now(timezone.utc)
        end = now - timedelta(days=8)
        start = end - timedelta(hours=1)
        (download,) = await self._add(_failed_download(
            program_start=start.replace(tzinfo=None),
            program_end=end.replace(tzinfo=None),
            start_timestamp=int(start.timestamp()),
            stop_timestamp=int(end.timestamp()),
        ))

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.retry_count, 0)
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_unknown_window_not_retried(self):
        """Unknown archive windows are conservative: no retry."""
        (download,) = await self._add(_failed_download())

        dm, _ = await self._run_sweep(
            archive_side_effect=NoCatchupSupportError("no catchup for channel")
        )

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.retry_count, 0)
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_zero_archive_days_not_retried(self):
        """A provider reporting no archive window means no retry."""
        (download,) = await self._add(_failed_download())

        dm, _ = await self._run_sweep(archive_days=0)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_flag_off_no_retries(self):
        """With auto_retry_failed_downloads disabled the sweep does nothing."""
        await self._set_flag(False)
        (download,) = await self._add(_failed_download())

        dm, archive_mock = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.retry_count, 0)
        self.assertEqual(dm._queue.qsize(), 0)
        self.assertEqual(archive_mock.await_count, 0,
                         "The provider must not be queried when the flag is off.")

    async def test_cancelled_schedule_not_revived(self):
        """A FAILED download whose linked schedule was CANCELLED by the user
        must not be auto-retried, and the schedule must stay CANCELLED."""
        (download,) = await self._add(_failed_download())
        (schedule,) = await self._add(ScheduledRecording(
            account_id=1,
            channel_id="ch1",
            channel_name="Test Channel",
            program_title="Failed Show",
            program_start=download.program_start,
            program_end=download.program_end,
            start_timestamp=download.start_timestamp,
            stop_timestamp=download.stop_timestamp,
            duration_minutes=60,
            status=ScheduledStatus.CANCELLED.value,
            status_message="Cancelled by user",
            download_id=download.id,
        ))

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value,
                         "A download under a cancelled schedule must not be retried.")
        self.assertEqual(refreshed.retry_count, 0)
        refreshed_schedule = await self._get_schedule(schedule.id)
        self.assertEqual(refreshed_schedule.status, ScheduledStatus.CANCELLED.value,
                         "Auto-retry must never revive a CANCELLED schedule.")
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_recent_failure_waits_for_interval(self):
        """Attempts must be spaced at least AUTO_RETRY_MIN_INTERVAL_MINUTES apart."""
        now = datetime.now(timezone.utc)
        (download,) = await self._add(_failed_download(
            completed_at=(now - timedelta(minutes=2)).replace(tzinfo=None),
        ))

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.retry_count, 0)
        self.assertEqual(dm._queue.qsize(), 0)

    async def test_vod_download_not_retried(self):
        """VOD downloads have no catchup window and are never auto-retried."""
        (download,) = await self._add(_failed_download(is_vod=True))

        dm, _ = await self._run_sweep(archive_days=7)

        refreshed = await self._get_download(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(dm._queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
