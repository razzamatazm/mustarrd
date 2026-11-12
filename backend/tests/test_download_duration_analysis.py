"""Raw download duration stays distinct from the final processed media."""
import asyncio
import math
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database import _apply_lightweight_migrations, _column_exists
from models import AppSettings, Download, DownloadStatus
from services.download_analysis import analyze_duration
from services.download_manager import DownloadManager


def recording(path="recording.ts", **overrides):
    values = dict(
        id=1, account_id=1, channel_id="12", channel_name="Example channel",
        program_title="Example program", program_start=datetime(2026, 10, 1, 12),
        program_end=datetime(2026, 10, 1, 13), duration_minutes=60,
        status=DownloadStatus.PENDING.value, output_path=path, source_url="http://example.test/stream",
        is_vod=False, progress=0, file_size=0, downloaded_bytes=0,
        created_at=datetime(2026, 10, 1, 14), retry_count=0,
    )
    values.update(overrides)
    return Download(**values)


def session_context(download, settings=None):
    session = AsyncMock()
    row = MagicMock()
    row.scalar_one_or_none.return_value = download
    settings_row = MagicMock()
    settings_row.scalar_one_or_none.return_value = settings
    session.execute.side_effect = [row, settings_row]

    @asynccontextmanager
    async def context():
        yield session

    return session, context


class DurationClassificationTests(unittest.TestCase):
    def test_matching_short_and_long_results(self):
        for actual, expected_status in ((3600, "matched"), (3240, "short"), (3900, "long")):
            with self.subTest(actual=actual):
                result = analyze_duration(3600, actual)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.actual_seconds, actual)

    def test_tolerance_is_seconds_and_does_not_grow_for_long_recordings(self):
        for expected in (60, 3600, 28800):
            for difference in (-5, 5):
                self.assertEqual(analyze_duration(expected, expected + difference).status, "matched")
            self.assertEqual(analyze_duration(expected, expected - 5.01).status, "short")
            self.assertEqual(analyze_duration(expected, expected + 5.01).status, "long")

    def test_short_program_with_five_minute_buffer_is_valid(self):
        self.assertEqual(analyze_duration(120, 420).status, "long")

    def test_unknown_probe_never_becomes_the_expected_duration(self):
        for measured in (None, 0, -1, math.nan, math.inf, -math.inf, True, "3600"):
            with self.subTest(measured=measured):
                result = analyze_duration(3600, measured)
                self.assertEqual(result.status, "unavailable")
                self.assertIsNone(result.actual_seconds)

    def test_broken_timestamp_estimate_is_visible_but_not_a_trusted_surplus(self):
        result = analyze_duration(3600, 26 * 3600)
        self.assertEqual(result.status, "unreliable")
        self.assertEqual(result.actual_seconds, 26 * 3600)
        self.assertIn("timestamps", result.reason)

    def test_invalid_expected_duration_is_not_comparable(self):
        for expected in (0, -60, math.nan, math.inf, None, True):
            with self.subTest(expected=expected):
                self.assertEqual(analyze_duration(expected, 3600).status, "unavailable")


class DurationObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = DownloadManager()
        self.manager._broadcast_log = AsyncMock()

    async def test_expected_range_includes_requested_padding(self):
        download = recording(duration_minutes=66)
        with patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3960)):
            await self.manager._analyze_downloaded_file(download)
        self.assertEqual(download.expected_duration_seconds, 3960)
        self.assertEqual(download.duration_analysis_status, "matched")
        self.assertEqual(download.to_dict()["raw_duration_seconds"], 3960)
        self.assertEqual(download.to_dict()["expected_duration_seconds"], 3960)

    async def test_original_measurement_survives_later_processing(self):
        download = recording()
        probe = AsyncMock(return_value=3900)
        with patch("services.post_processor.post_processor._get_duration", probe):
            await self.manager._analyze_downloaded_file(download)
            download.recorded_duration_seconds = 2700
            await self.manager._analyze_downloaded_file(download, raw_available=False)
        probe.assert_awaited_once()
        self.assertEqual(download.raw_duration_seconds, 3900)
        self.assertEqual(download.duration_analysis_status, "long")
        self.assertEqual(download.recorded_duration_seconds, 2700)

    async def test_unknown_processing_history_is_not_measured_as_raw(self):
        download = recording(status=DownloadStatus.PROCESSING.value, path="finished.mkv")
        with patch("services.post_processor.post_processor._get_duration", AsyncMock()) as probe:
            await self.manager._analyze_downloaded_file(download, raw_available=False)
        probe.assert_not_awaited()
        self.assertIsNone(download.raw_duration_seconds)
        self.assertEqual(download.duration_analysis_status, "unavailable")

    async def test_probe_failure_is_informational(self):
        download = recording()
        with patch("services.post_processor.post_processor._get_duration", AsyncMock(side_effect=OSError("unreadable"))):
            await self.manager._analyze_downloaded_file(download)
        self.assertEqual(download.status, DownloadStatus.PENDING.value)
        self.assertEqual(download.duration_analysis_status, "unavailable")
        self.assertIsNone(download.raw_duration_seconds)

    async def test_vod_is_excluded(self):
        download = recording(is_vod=True)
        with patch("services.post_processor.post_processor._get_duration", AsyncMock()) as probe:
            await self.manager._analyze_downloaded_file(download)
        probe.assert_not_awaited()
        self.assertIsNone(download.expected_duration_seconds)
        self.assertIsNone(download.duration_analysis_status)

    async def test_missing_expected_duration_is_explicitly_unavailable(self):
        download = recording(duration_minutes=None)
        with patch("services.post_processor.post_processor._get_duration", AsyncMock()) as probe:
            await self.manager._analyze_downloaded_file(download)
        probe.assert_not_awaited()
        self.assertEqual(download.duration_analysis_status, "unavailable")
        self.assertIsNone(download.raw_duration_seconds)
        self.assertIsNone(download.expected_duration_seconds)

    async def test_new_attempt_forgets_all_previous_duration_evidence(self):
        download = recording(raw_duration_seconds=3000, duration_analysis_status="short", duration_analysis_reason="old")
        self.manager._reset_duration_analysis(download)
        self.assertIsNone(download.raw_duration_seconds)
        self.assertIsNone(download.duration_analysis_status)
        self.assertIsNone(download.duration_analysis_reason)


class DurationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_raw_transfer_recovery_analyzes_before_queue_or_move(self):
        for transcode_enabled in (False, True):
            with self.subTest(transcode_enabled=transcode_enabled), tempfile.TemporaryDirectory() as folder:
                raw_folder = Path(folder) / "downloads"
                raw_folder.mkdir()
                completed_folder = Path(folder) / "completed"
                completed_folder.mkdir()
                path = raw_folder / "recording.ts"
                path.write_bytes(b"raw media")
                download = recording(
                    str(path), status=DownloadStatus.DOWNLOADING.value,
                    file_size=path.stat().st_size, downloaded_bytes=path.stat().st_size,
                    progress=100,
                )
                settings = AppSettings(
                    download_folder=str(raw_folder), completed_folder=str(completed_folder),
                    transcode_enabled=transcode_enabled, comskip_enabled=False,
                )
                session = AsyncMock()
                settings_row = MagicMock()
                settings_row.scalar_one_or_none.return_value = settings
                downloads_row = MagicMock()
                downloads_row.scalars.return_value.all.return_value = [download]
                session.execute.side_effect = [settings_row, downloads_row]

                @asynccontextmanager
                async def context():
                    yield session

                async def evidence_is_ready(*args, **kwargs):
                    self.assertEqual(download.raw_duration_seconds, 3900)
                    self.assertEqual(download.duration_analysis_status, "long")
                    return str(completed_folder / path.name)

                manager = DownloadManager()
                manager._broadcast_log = AsyncMock()
                manager._transition_processing = AsyncMock(side_effect=evidence_is_ready)
                manager._transition_completed = AsyncMock(side_effect=evidence_is_ready)
                manager._move_to_completed_async = AsyncMock(side_effect=evidence_is_ready)
                manager._write_nfo_sidecar = AsyncMock()
                session.commit.side_effect = evidence_is_ready
                probe = AsyncMock(return_value=3900)
                with (
                    patch("services.download_manager.async_session_maker", context),
                    patch("services.post_processor.post_processor._get_duration", probe),
                ):
                    await manager.recover_incomplete_downloads()
                probe.assert_awaited_once_with(str(path))
                session.commit.assert_awaited_once()
                if transcode_enabled:
                    manager._transition_processing.assert_awaited_once()
                    self.assertEqual(manager._post_queue.get_nowait(), download.id)
                    manager._move_to_completed_async.assert_not_awaited()
                else:
                    manager._move_to_completed_async.assert_awaited_once()
                    manager._transition_completed.assert_awaited_once()
                self.assertEqual(path.read_bytes(), b"raw media")

    async def test_legacy_processing_recovery_does_not_probe_raw_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            raw_folder = Path(folder) / "downloads"
            raw_folder.mkdir()
            path = raw_folder / "recording.ts"
            path.write_bytes(b"possibly already processed media")
            download = recording(str(path), status=DownloadStatus.PROCESSING.value)
            settings = AppSettings(
                download_folder=str(raw_folder), completed_folder=str(Path(folder) / "completed"),
                transcode_enabled=True, comskip_enabled=False,
            )
            session = AsyncMock()
            settings_row = MagicMock()
            settings_row.scalar_one_or_none.return_value = settings
            downloads_row = MagicMock()
            downloads_row.scalars.return_value.all.return_value = [download]
            session.execute.side_effect = [settings_row, downloads_row]

            @asynccontextmanager
            async def context():
                yield session

            manager = DownloadManager()
            manager._broadcast_log = AsyncMock()
            manager._broadcast_progress = AsyncMock()
            with (
                patch("services.download_manager.async_session_maker", context),
                patch("services.post_processor.post_processor._get_duration", AsyncMock()) as probe,
            ):
                await manager.recover_incomplete_downloads()
            probe.assert_not_awaited()
            self.assertEqual(download.duration_analysis_status, "unavailable")
            self.assertIsNone(download.raw_duration_seconds)
            self.assertEqual(download.status, DownloadStatus.PROCESSING.value)
            self.assertEqual(manager._post_queue.get_nowait(), download.id)
            session.commit.assert_awaited_once()

    async def test_retry_clears_duration_evidence_before_requeueing(self):
        for status in (DownloadStatus.FAILED.value, DownloadStatus.CANCELLED.value, DownloadStatus.INTERRUPTED.value):
            with self.subTest(status=status):
                download = recording(
                    status=status, raw_duration_seconds=3240, duration_analysis_status="short",
                    duration_analysis_reason="previous attempt", interruption_reason="Connection dropped",
                )
                session, context = session_context(download)
                manager = DownloadManager()
                manager._sync_schedule_status = AsyncMock()
                manager._recapture_path = AsyncMock(return_value="new-attempt.ts")

                async def committed():
                    self.assertIsNone(download.raw_duration_seconds)
                    self.assertIsNone(download.duration_analysis_status)
                    self.assertIsNone(download.duration_analysis_reason)
                    self.assertEqual(download.status, DownloadStatus.PENDING.value)

                session.commit.side_effect = committed
                with patch("services.download_manager.async_session_maker", context):
                    self.assertTrue(await manager.retry_download(download.id))
                session.commit.assert_awaited_once()
                self.assertEqual(manager._queue.get_nowait(), download.id)

    async def test_analysis_precedes_queue_commit_and_never_changes_status_by_itself(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recording.ts"
            path.write_bytes(b"raw media")
            download = recording(str(path))
            settings = AppSettings(transcode_enabled=True, comskip_enabled=False)
            session, context = session_context(download, settings)
            manager = DownloadManager()
            manager._broadcast_log = AsyncMock()
            manager._transition_downloading = AsyncMock()
            manager._download_catchup_stream = AsyncMock(return_value=path.stat().st_size)

            async def queued(*args, **kwargs):
                self.assertEqual(download.raw_duration_seconds, 3240)
                self.assertEqual(download.duration_analysis_status, "short")
                self.assertIsNone(download.interruption_reason)
                download.status = DownloadStatus.PROCESSING.value

            manager._transition_processing = AsyncMock(side_effect=queued)
            with (
                patch("services.download_manager.async_session_maker", context),
                patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3240)),
            ):
                await manager._execute_download(download.id)
            manager._transition_processing.assert_awaited_once()
            self.assertEqual(await manager._post_queue.get(), download.id)
            self.assertEqual(path.read_bytes(), b"raw media")

    async def test_no_processing_still_analyzes_before_the_final_move(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recording.ts"
            path.write_bytes(b"raw media")
            download = recording(str(path))
            settings = AppSettings(transcode_enabled=False, comskip_enabled=False)
            session, context = session_context(download, settings)
            manager = DownloadManager()
            manager._broadcast_log = AsyncMock()
            manager._transition_downloading = AsyncMock()
            manager._download_catchup_stream = AsyncMock(return_value=path.stat().st_size)

            async def moved(*args):
                self.assertEqual(download.raw_duration_seconds, 3900)
                return str(path)

            manager._move_to_completed_async = AsyncMock(side_effect=moved)
            manager._store_recorded_duration = AsyncMock()
            manager._write_nfo_sidecar = AsyncMock()
            manager._integrity_check_warning = AsyncMock(return_value=None)
            manager._transition_completed = AsyncMock()
            manager._trigger_plex_refresh = AsyncMock()
            with (
                patch("services.download_manager.async_session_maker", context),
                patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3900)),
            ):
                await manager._execute_download(download.id)
            manager._move_to_completed_async.assert_awaited_once()
            self.assertEqual(download.duration_analysis_status, "long")

    async def test_playable_interrupted_capture_is_analyzed_before_processing(self):
        download = recording()
        manager = DownloadManager()
        manager._broadcast_log = AsyncMock()

        async def queued(*args, **kwargs):
            self.assertEqual(download.raw_duration_seconds, 3240)
            self.assertEqual(download.interruption_reason, "Connection dropped")

        manager._transition_processing = AsyncMock(side_effect=queued)
        settings = AppSettings(transcode_enabled=True)
        with patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3240)):
            await manager._finalize_interrupted(AsyncMock(), download, 1, settings, "Connection dropped")
        manager._transition_processing.assert_awaited_once()


class DurationMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_database_adds_nullable_evidence_idempotently(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                for table in ("plex_servers", "app_settings", "epg_programs", "xtream_accounts", "scheduled_recordings", "downloads"):
                    await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                await conn.execute(text("INSERT INTO downloads (id) VALUES (1)"))
                await _apply_lightweight_migrations(conn)
                await _apply_lightweight_migrations(conn)
                for column in ("raw_duration_seconds", "duration_analysis_status", "duration_analysis_reason"):
                    self.assertTrue(await _column_exists(conn, "downloads", column))
                row = (await conn.execute(text("SELECT raw_duration_seconds, duration_analysis_status, duration_analysis_reason FROM downloads"))).one()
                self.assertEqual(tuple(row), (None, None, None))
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
