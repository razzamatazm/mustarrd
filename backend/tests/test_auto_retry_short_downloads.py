"""Short classification uses the existing failed-download master retry switch."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from models import AppSettings, Download, DownloadStatus, ScheduledRecording, ScheduledStatus, XtreamAccount
from services.download_manager import DownloadManager
from services.epg_service import NoCatchupSupportError
from services.scheduled_manager import AUTO_RETRY_MAX_ATTEMPTS, ScheduledManager
from tests.test_short_download_retry import recording


class AutoRetryShortDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add(XtreamAccount(id=1, name="Example", server_url="http://example.test", username="u", password="p"))
            session.add(AppSettings(
                download_folder=str(self.root / "downloads"), completed_folder=str(self.root / "completed"),
                min_free_space_gb=1, mark_short_downloads_failed=True, auto_retry_failed_downloads=True,
            ))
            await session.commit()
        self.scheduler = ScheduledManager()
        self.manager = DownloadManager()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def add_download(self, **overrides):
        values = dict(
            status=DownloadStatus.FAILED.value, short_file_failed=True, raw_duration_seconds=3240,
            duration_analysis_status="short", interruption_reason="Measured short recording",
            completed_at=datetime.utcnow() - timedelta(minutes=30), retry_count=0,
        )
        values.update(overrides)
        download = recording(self.root / "completed" / "show.ts", **values)
        async with self.sessions() as session:
            session.add(download)
            await session.commit()
        return download

    async def read(self, download_id):
        async with self.sessions() as session:
            return await session.get(Download, download_id)

    async def set_flags(self, failed, short):
        async with self.sessions() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            settings.auto_retry_failed_downloads = failed
            settings.mark_short_downloads_failed = short
            await session.commit()

    async def sweep(self, *, archive_days=7, archive_error=None, free_gb=1000, before_retry=None):
        archive = AsyncMock(return_value=archive_days, side_effect=archive_error)
        original_retry = self.manager.retry_download

        async def retry(download_id, **kwargs):
            if before_retry is not None:
                await before_retry(download_id)
            return await original_retry(download_id, **kwargs)

        with (
            patch("services.scheduled_manager.async_session_maker", self.sessions),
            patch("services.download_manager.async_session_maker", self.sessions),
            patch("services.scheduled_manager.download_manager", self.manager),
            patch.object(self.scheduler, "_get_free_space_gb", AsyncMock(return_value=free_gb)),
            patch.object(self.scheduler, "_get_archive_days_raw", archive),
            patch.object(self.manager, "retry_download", AsyncMock(side_effect=retry)),
        ):
            await self.scheduler._auto_retry_failed_downloads()
        return archive

    async def test_all_four_switch_combinations_require_master(self):
        for failed_enabled in (False, True):
            for short_enabled in (False, True):
                with self.subTest(failed=failed_enabled, short=short_enabled):
                    async with self.sessions() as session:
                        await session.execute(delete(Download))
                        await session.commit()
                    self.manager = DownloadManager()
                    await self.set_flags(failed_enabled, short_enabled)
                    failed = await self.add_download(duration_analysis_status=None, short_file_failed=False)
                    short = await self.add_download()
                    await self.sweep()
                    failed_after = await self.read(failed.id)
                    short_after = await self.read(short.id)
                    self.assertEqual(failed_after.status, "pending" if failed_enabled else "failed")
                    self.assertEqual(short_after.status, "pending" if failed_enabled and short_enabled else "failed")
                    self.assertEqual(self.manager._queue.qsize(), int(failed_enabled) + int(failed_enabled and short_enabled))
                    if failed_enabled and short_enabled:
                        self.assertIsNone(short_after.raw_duration_seconds)
                        self.assertEqual(short_after.retry_count, 1)
                        self.assertEqual(short_after.source_url, short.source_url)

    async def test_master_policy_retries_after_commercial_processing_fails(self):
        path = self.root / "downloads" / "show.ts"
        path.parent.mkdir()
        payload = b"usable short recording retained after processing error"
        path.write_bytes(payload)
        download = await self.add_download(
            output_path=str(path), status=DownloadStatus.PENDING.value,
            raw_duration_seconds=None, duration_analysis_status=None,
            interruption_reason=None, completed_at=None,
        )
        async with self.sessions() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            settings.mark_short_downloads_failed = False
            settings.comskip_enabled = True
            settings.transcode_enabled = True
            settings.write_nfo_files = False
            await session.commit()
        self.manager._download_catchup_stream = AsyncMock(return_value=len(payload))
        self.manager._post_process = AsyncMock(side_effect=RuntimeError("Commercial detection failed"))
        with (
            patch("services.download_manager.async_session_maker", self.sessions),
            patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3240)),
        ):
            await self.manager._execute_download(download.id)
            self.assertEqual(self.manager._post_queue.get_nowait(), download.id)
            await self.manager._execute_post_process(download.id)
        failed = await self.read(download.id)
        self.assertEqual(failed.status, DownloadStatus.FAILED.value)
        self.assertEqual(failed.duration_analysis_status, "short")
        self.assertIn("Commercial detection failed", failed.error_message)
        # The short-file option is off, so nothing marks the shortfall and
        # the retry behaves exactly as an ordinary failed download does.
        self.assertIsNone(failed.interruption_reason)
        self.assertFalse(failed.short_file_failed)
        self.assertEqual(path.read_bytes(), payload)
        async with self.sessions() as session:
            row = await session.get(Download, download.id)
            row.completed_at = datetime.utcnow() - timedelta(minutes=30)
            await session.commit()
        await self.sweep()
        retried = await self.read(download.id)
        self.assertEqual(retried.status, DownloadStatus.PENDING.value)
        self.assertEqual(retried.retry_count, 1)
        self.assertEqual(self.manager._queue.qsize(), 1)
        self.assertEqual(retried.output_path, str(path))
        self.assertEqual(retried.source_url, download.source_url)
        self.assertIsNone(retried.duration_analysis_status)
        self.assertIsNone(retried.interruption_reason)

    async def test_retry_cap_and_cooldown_apply_to_short_recordings(self):
        capped = await self.add_download(retry_count=AUTO_RETRY_MAX_ATTEMPTS)
        recent = await self.add_download(completed_at=datetime.utcnow() - timedelta(minutes=2))
        recently_retried = await self.add_download(last_retry_at=datetime.utcnow() - timedelta(minutes=2))
        await self.sweep()
        self.assertEqual(self.manager._queue.qsize(), 0)
        for download in (capped, recent, recently_retried):
            refreshed = await self.read(download.id)
            self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
            self.assertEqual(refreshed.retry_count, download.retry_count)

    async def test_nonshort_unknown_vod_and_cancelled_rows_are_ineligible(self):
        downloads = []
        for status in (None, "matched", "long", "unavailable", "unreliable"):
            downloads.append(await self.add_download(duration_analysis_status=status, status=DownloadStatus.INTERRUPTED.value))
        downloads.append(await self.add_download(is_vod=True))
        downloads.append(await self.add_download(status=DownloadStatus.CANCELLED.value))
        downloads.append(await self.add_download(status=DownloadStatus.COMPLETED.value))
        archive = await self.sweep()
        archive.assert_not_awaited()
        self.assertEqual(self.manager._queue.qsize(), 0)
        for download in downloads:
            self.assertEqual((await self.read(download.id)).status, download.status)

    async def test_unknown_or_expired_archive_blocks_retry(self):
        expired_end = datetime.now(timezone.utc) - timedelta(days=8)
        expired = await self.add_download(
            stop_timestamp=int(expired_end.timestamp()),
            program_end=expired_end.replace(tzinfo=None),
        )
        await self.sweep()
        self.assertEqual((await self.read(expired.id)).retry_count, 0)
        eligible = await self.add_download()
        for args in (
            {"archive_days": 0},
            {"archive_error": NoCatchupSupportError("unavailable")},
            {"archive_error": RuntimeError("provider unreachable")},
        ):
            with self.subTest(args=args):
                self.scheduler = ScheduledManager()
                await self.sweep(**args)
                self.assertEqual((await self.read(eligible.id)).retry_count, 0)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_low_or_unknown_disk_space_does_not_spend_attempts(self):
        download = await self.add_download()
        for free_gb in (None, 0):
            with self.subTest(free_gb=free_gb):
                archive = await self.sweep(free_gb=free_gb)
                archive.assert_not_awaited()
                self.assertEqual((await self.read(download.id)).retry_count, 0)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def add_schedule(self, download, status):
        async with self.sessions() as session:
            schedule = ScheduledRecording(
                account_id=1, channel_id="12", channel_name="Example", program_title="Example program",
                program_start=download.program_start, program_end=download.program_end,
                duration_minutes=60, download_id=download.id, status=status,
            )
            session.add(schedule)
            await session.commit()
        return schedule

    async def test_any_cancelled_or_completed_linked_schedule_blocks_retry(self):
        for terminal in (ScheduledStatus.CANCELLED.value, ScheduledStatus.COMPLETED.value):
            with self.subTest(terminal=terminal):
                download = await self.add_download()
                # Insert the blocking row first: the old last-row dictionary
                # would keep only the later Interrupted schedule and miss it.
                protected = await self.add_schedule(download, terminal)
                await self.add_schedule(download, ScheduledStatus.INTERRUPTED.value)
                await self.sweep()
                self.assertEqual((await self.read(download.id)).retry_count, 0)
                async with self.sessions() as session:
                    self.assertEqual((await session.get(ScheduledRecording, protected.id)).status, terminal)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_download_cancelled_after_selection_is_not_revived(self):
        download = await self.add_download()

        async def cancel(download_id):
            async with self.sessions() as session:
                row = await session.get(Download, download_id)
                row.status = DownloadStatus.CANCELLED.value
                await session.commit()

        await self.sweep(before_retry=cancel)
        self.assertEqual((await self.read(download.id)).status, DownloadStatus.CANCELLED.value)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_schedule_cancelled_after_selection_is_not_revived(self):
        download = await self.add_download()
        schedule = await self.add_schedule(download, ScheduledStatus.INTERRUPTED.value)

        async def cancel(download_id):
            async with self.sessions() as session:
                row = await session.get(ScheduledRecording, schedule.id)
                row.status = ScheduledStatus.CANCELLED.value
                await session.commit()

        await self.sweep(before_retry=cancel)
        self.assertEqual((await self.read(download.id)).status, DownloadStatus.FAILED.value)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_setting_disabled_after_selection_prevents_retry(self):
        download = await self.add_download()

        async def disable(download_id):
            await self.set_flags(False, False)

        await self.sweep(before_retry=disable)
        self.assertEqual((await self.read(download.id)).status, DownloadStatus.FAILED.value)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_changed_failure_kind_requires_its_own_enabled_setting(self):
        download = await self.add_download()

        async def fail(download_id):
            await self.set_flags(False, True)
            async with self.sessions() as session:
                row = await session.get(Download, download_id)
                row.status = DownloadStatus.FAILED.value
                row.duration_analysis_status = None
                row.raw_duration_seconds = None
                await session.commit()

        await self.sweep(before_retry=fail)
        self.assertEqual((await self.read(download.id)).status, DownloadStatus.FAILED.value)
        self.assertEqual(self.manager._queue.qsize(), 0)

    async def test_setting_disabled_during_recapture_allocation_prevents_retry(self):
        download = await self.add_download()
        original = self.manager._recapture_path

        async def allocate(row):
            path = await original(row)
            await self.set_flags(False, False)
            return path

        with patch.object(self.manager, "_recapture_path", AsyncMock(side_effect=allocate)):
            await self.sweep()
        self.assertEqual(self.manager._queue.qsize(), 0)
        refreshed = await self.read(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.output_path, download.output_path)
        self.assertEqual(refreshed.raw_duration_seconds, 3240)

    async def test_cancellation_during_recapture_allocation_keeps_its_marker(self):
        download = await self.add_download()
        original = self.manager._recapture_path

        async def allocate(row):
            path = await original(row)
            self.manager._cancelled.add(row.id)
            return path

        with patch.object(self.manager, "_recapture_path", AsyncMock(side_effect=allocate)):
            await self.sweep()
        self.assertIn(download.id, self.manager._cancelled)
        self.assertEqual(self.manager._queue.qsize(), 0)
        refreshed = await self.read(download.id)
        self.assertEqual(refreshed.status, DownloadStatus.FAILED.value)
        self.assertEqual(refreshed.output_path, download.output_path)
        self.assertEqual(refreshed.raw_duration_seconds, 3240)

    async def test_cancellation_during_pending_transition_wins_before_enqueue(self):
        download = await self.add_download()
        original = self.manager._transition_pending

        async def pending(session, row, **kwargs):
            result = await original(session, row, **kwargs)
            self.manager._cancelled.add(row.id)
            return result

        with patch.object(self.manager, "_transition_pending", AsyncMock(side_effect=pending)):
            await self.sweep()
        self.assertIn(download.id, self.manager._cancelled)
        self.assertEqual(self.manager._queue.qsize(), 0)
        self.assertEqual((await self.read(download.id)).status, DownloadStatus.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
