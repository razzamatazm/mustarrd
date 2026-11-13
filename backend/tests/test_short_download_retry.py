"""Short HTTP-success captures are retained and retried only by explicit policy."""
import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.settings import SettingsUpdate, get_settings, update_settings
from database import Base, _apply_lightweight_migrations
from models import AppSettings, Download, DownloadStatus, ScheduledRecording, ScheduledStatus, XtreamAccount
from services.download_manager import DownloadManager


def recording(path, **overrides):
    end = datetime.now(timezone.utc) - timedelta(hours=1)
    start = end - timedelta(hours=1)
    values = dict(
        account_id=1, channel_id="12", channel_name="Example channel",
        program_title="Example program", program_start=start.replace(tzinfo=None),
        program_end=end.replace(tzinfo=None), start_timestamp=int(start.timestamp()),
        stop_timestamp=int(end.timestamp()), duration_minutes=60,
        status=DownloadStatus.PENDING.value, output_path=str(path),
        source_url="http://example.test/timeshift/user/pass/60/2026-10-01:12-00/12.ts",
        is_vod=False, progress=0, file_size=0, downloaded_bytes=0,
    )
    values.update(overrides)
    return Download(**values)


class ShortDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / "downloads"
        self.completed = self.root / "completed"
        self.downloads.mkdir()
        self.completed.mkdir()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add(XtreamAccount(id=1, name="Example", server_url="http://example.test", username="user", password="pass"))
            session.add(AppSettings(
                download_folder=str(self.downloads), completed_folder=str(self.completed),
                transcode_enabled=False, comskip_enabled=False, write_nfo_files=False,
                min_free_space_gb=1,
            ))
            await session.commit()
        self.manager = DownloadManager()
        self.manager._broadcast_log = AsyncMock()
        self.manager._broadcast_progress = AsyncMock()
        self.manager._trigger_plex_refresh = AsyncMock()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def add(self, row):
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return row

    async def read(self, download_id):
        async with self.sessions() as session:
            return await session.get(Download, download_id)

    async def test_new_installation_get_and_update_preserve_setting_independence(self):
        with (
            patch("api.settings.is_docker_env", return_value=False),
            patch("api.settings.is_desktop_env", return_value=False),
            patch("api.settings.ensure_config_files", return_value=self.root),
        ):
            async with self.sessions() as session:
                payload = await get_settings(None, session)
                self.assertIs(payload["mark_short_downloads_failed"], False)
                self.assertIs(payload["auto_retry_failed_downloads"], False)
                payload = await update_settings(SettingsUpdate(mark_short_downloads_failed=True), None, session)
                self.assertIs(payload["mark_short_downloads_failed"], True)
                self.assertIs(payload["auto_retry_failed_downloads"], False)
                payload = await update_settings(SettingsUpdate(auto_retry_failed_downloads=True), None, session)
                self.assertIs(payload["mark_short_downloads_failed"], True)
                payload = await update_settings(SettingsUpdate(mark_short_downloads_failed=None), None, session)
                self.assertIs(payload["mark_short_downloads_failed"], True)
                payload = await update_settings(SettingsUpdate(mark_short_downloads_failed=False), None, session)
                self.assertIs(payload["mark_short_downloads_failed"], False)
                self.assertIs(payload["auto_retry_failed_downloads"], True)

    async def test_migration_defaults_existing_installations_to_disabled(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                for table in ("plex_servers", "app_settings", "epg_programs", "xtream_accounts", "scheduled_recordings", "downloads"):
                    await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                await conn.execute(text("INSERT INTO app_settings (id) VALUES (1)"))
                await _apply_lightweight_migrations(conn)
                await _apply_lightweight_migrations(conn)
                value = (await conn.execute(text("SELECT mark_short_downloads_failed FROM app_settings"))).scalar_one()
                self.assertEqual(value, 0)
        finally:
            await engine.dispose()

    async def test_successful_short_capture_is_kept_and_schedule_is_interrupted(self):
        path = self.downloads / "show.ts"
        payload = b"available raw recording"
        download = await self.add(recording(path))
        schedule = await self.add(ScheduledRecording(
            account_id=1, channel_id="12", channel_name="Example channel", program_title="Example program",
            program_start=download.program_start, program_end=download.program_end,
            duration_minutes=60, status=ScheduledStatus.QUEUED.value, download_id=download.id,
        ))

        async def capture(*args, **kwargs):
            path.write_bytes(payload)
            return len(payload)

        self.manager._download_catchup_stream = AsyncMock(side_effect=capture)
        self.manager._integrity_check_warning = AsyncMock(return_value=None)
        with (
            patch("services.download_manager.async_session_maker", self.sessions),
            patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3240)),
        ):
            await self.manager._execute_download(download.id)
        finished = await self.read(download.id)
        self.assertEqual(finished.status, DownloadStatus.INTERRUPTED.value)
        self.assertIn("0:54:00", finished.interruption_reason)
        self.assertIn("1:00:00", finished.interruption_reason)
        self.assertIn("including padding", finished.interruption_reason)
        self.assertEqual(Path(finished.output_path).read_bytes(), payload)
        self.assertEqual(finished.source_url, download.source_url)
        self.assertEqual(finished.raw_duration_seconds, 3240)
        self.assertEqual(finished.retry_count, 0)
        async with self.sessions() as session:
            linked = await session.get(ScheduledRecording, schedule.id)
            self.assertEqual(linked.status, ScheduledStatus.INTERRUPTED.value)

    async def test_shortfall_is_recorded_before_commercial_processing_and_survives_it(self):
        path = self.downloads / "show.ts"
        path.write_bytes(b"available raw recording")
        download = await self.add(recording(path, duration_minutes=66))
        async with self.sessions() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            settings.transcode_enabled = True
            settings.comskip_enabled = True
            await session.commit()
        self.manager._download_catchup_stream = AsyncMock(return_value=path.stat().st_size)
        self.manager._integrity_check_warning = AsyncMock(return_value=None)

        # **kwargs: main now also passes expected_duration_seconds (#462).
        async def process(input_path, download_id, session, settings, **kwargs):
            current = await session.get(Download, download_id)
            self.assertEqual(current.raw_duration_seconds, 3840)
            self.assertIn("1:06:00", current.interruption_reason)
            self.assertTrue(settings.comskip_enabled)
            return input_path, []

        self.manager._post_process = AsyncMock(side_effect=process)
        with (
            patch("services.download_manager.async_session_maker", self.sessions),
            patch("services.post_processor.post_processor._get_duration", AsyncMock(side_effect=[3840, 2700])),
        ):
            await self.manager._execute_download(download.id)
            self.assertEqual(self.manager._post_queue.get_nowait(), download.id)
            await self.manager._execute_post_process(download.id)
        finished = await self.read(download.id)
        self.assertEqual(finished.status, DownloadStatus.INTERRUPTED.value)
        self.assertEqual(finished.raw_duration_seconds, 3840)
        self.assertEqual(finished.recorded_duration_seconds, 2700)
        self.assertTrue(Path(finished.output_path).is_file())

    async def test_nonshort_unknown_and_vod_do_not_gain_an_interruption_reason(self):
        cases = [(3600, False), (3595, False), (3900, False), (None, False), (26 * 3600, False), (3000, True)]
        for actual, is_vod in cases:
            with self.subTest(actual=actual, is_vod=is_vod):
                download = recording(self.downloads / "show.ts", is_vod=is_vod)
                with patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=actual)):
                    await self.manager._prepare_downloaded_file(download)
                self.assertIsNone(download.interruption_reason)

    async def test_existing_network_interruption_reason_is_preserved(self):
        download = recording(self.downloads / "show.ts", interruption_reason="Provider connection dropped")
        with patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=3240)):
            await self.manager._prepare_downloaded_file(download)
        self.assertEqual(download.interruption_reason, "Provider connection dropped")
        self.assertEqual(download.duration_analysis_status, "short")

    async def test_legacy_processed_file_is_never_classified_short_from_its_final_length(self):
        download = recording(self.completed / "show.mkv", status=DownloadStatus.PROCESSING.value)
        with patch("services.post_processor.post_processor._get_duration", AsyncMock(return_value=2700)) as probe:
            await self.manager._prepare_downloaded_file(download, raw_available=False)
        probe.assert_not_awaited()
        self.assertIsNone(download.interruption_reason)
        self.assertEqual(download.duration_analysis_status, "unavailable")

    async def test_recapture_avoids_same_directory_and_processed_output_collisions(self):
        for extension in (".ts", ".mkv"):
            with self.subTest(extension=extension):
                retained = self.completed / f"show{extension}"
                retained.write_bytes(b"kept recording")
                download = recording(retained, status=DownloadStatus.INTERRUPTED.value)
                settings = AppSettings(download_folder=str(self.completed), completed_folder=str(self.completed))
                with patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=settings)):
                    candidate = await self.manager._recapture_path(download)
                self.assertNotEqual(Path(candidate).stem, retained.stem)
                Path(candidate).write_bytes(b"new capture")
                Path(candidate).with_suffix(extension).write_bytes(b"new processed media")
                self.assertEqual(retained.read_bytes(), b"kept recording")

    async def test_recapture_avoids_symlinked_folders_and_existing_working_media(self):
        retained = self.completed / "show.mkv"
        retained.write_bytes(b"kept recording")
        alias = self.root / "alias"
        alias.symlink_to(self.completed, target_is_directory=True)
        download = recording(retained, status=DownloadStatus.INTERRUPTED.value)
        settings = AppSettings(download_folder=str(alias), completed_folder=str(self.completed))
        with patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=settings)):
            candidate = await self.manager._recapture_path(download)
        self.assertNotEqual(Path(candidate).stem, retained.stem)
        Path(candidate).write_bytes(b"existing working file")
        with patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=settings)):
            next_candidate = await self.manager._recapture_path(download)
        self.assertNotEqual(next_candidate, candidate)
        settings.download_folder = str(self.downloads)
        existing = self.downloads / "show.ts"
        existing.write_bytes(b"another attempt")
        with patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=settings)):
            candidate = await self.manager._recapture_path(download)
        self.assertNotEqual(candidate, str(existing))
        self.assertEqual(existing.read_bytes(), b"another attempt")
        self.assertEqual(retained.read_bytes(), b"kept recording")

    async def test_settings_failure_cannot_return_the_retained_path_for_capture(self):
        retained = self.completed / "show.ts"
        retained.write_bytes(b"kept recording")
        with patch.object(self.manager, "_load_app_settings", AsyncMock(side_effect=RuntimeError("unavailable"))):
            candidate = await self.manager._recapture_path(recording(retained))
        self.assertNotEqual(candidate, str(retained))

    async def test_failed_and_cancelled_retry_leave_the_kept_file_untouched(self):
        for error in (RuntimeError("Provider refused request"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                retained = self.completed / f"kept-{type(error).__name__}.ts"
                retained.write_bytes(b"kept recording")
                download = await self.add(recording(
                    retained, status=DownloadStatus.INTERRUPTED.value,
                    duration_analysis_status="short", raw_duration_seconds=3240,
                    interruption_reason="Short recording",
                ))
                async with self.sessions() as session:
                    settings = (await session.execute(select(AppSettings))).scalar_one()
                    settings.download_folder = str(self.completed)
                    await session.commit()
                with patch("services.download_manager.async_session_maker", self.sessions):
                    self.assertTrue(await self.manager.retry_download(download.id))
                    queued = await self.read(download.id)
                    self.assertNotEqual(queued.output_path, str(retained))
                    self.assertIsNone(queued.raw_duration_seconds)

                    async def failed_capture(*args, **kwargs):
                        Path(queued.output_path).write_bytes(b"failed new attempt")
                        raise error

                    self.manager._download_catchup_stream = AsyncMock(side_effect=failed_capture)
                    self.manager._partial_is_playable = AsyncMock(return_value=False)
                    await self.manager._execute_download(download.id)
                self.assertEqual(retained.read_bytes(), b"kept recording")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
    async def test_real_media_successful_short_response_is_retained(self):
        source = self.root / "provider-response.ts"
        subprocess.run([
            shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=black:s=32x32:r=1", "-t", "50", "-c:v", "mpeg2video",
            "-an", "-f", "mpegts", str(source),
        ], check=True, timeout=15, capture_output=True)
        path = self.downloads / "real-short.ts"
        download = await self.add(recording(
            path, duration_minutes=1,
            source_url="http://example.test/timeshift/user/pass/1/2026-10-01:12-00/12.ts",
        ))

        async def capture(*args, **kwargs):
            shutil.copyfile(source, path)
            return path.stat().st_size

        self.manager._download_catchup_stream = AsyncMock(side_effect=capture)
        with patch("services.download_manager.async_session_maker", self.sessions):
            await self.manager._execute_download(download.id)
        finished = await self.read(download.id)
        self.assertEqual(finished.status, DownloadStatus.INTERRUPTED.value)
        self.assertAlmostEqual(finished.raw_duration_seconds, 50, delta=1)
        self.assertEqual(finished.expected_duration_seconds, 60)
        self.assertEqual(Path(finished.output_path).read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
