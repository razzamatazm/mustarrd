"""The optional trim is independent and always precedes other processing."""
import asyncio
import tempfile
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.settings import SettingsUpdate, update_settings
from database import Base, _apply_lightweight_migrations
from models import AppSettings, Download, DownloadStatus
from services.download_manager import DownloadManager
from services.post_processor import HardwareAccel


class BufferTrimPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / "downloads" / "Recording.ts"
        self.source.parent.mkdir()
        self.source.write_bytes(b"original recording")
        self.manager = DownloadManager()
        self.settings = AppSettings(
            download_folder=str(self.source.parent),
            completed_folder=str(Path(self.folder.name) / "completed"),
            min_free_space_gb=0,
            trim_dispatcharr_buffer=True,
            transcode_enabled=False,
            comskip_enabled=False,
            integrity_check_enabled=False,
            write_nfo_files=False,
        )
        self.download = Download(
            account_id=1, channel_id="100", channel_name="Channel", program_title="Recording",
            program_start=datetime(2026, 10, 21, 18), program_end=datetime(2026, 10, 21, 19),
            duration_minutes=66, source_url="http://provider.test/timeshift/1.ts",
            output_path=str(self.source), status=DownloadStatus.PROCESSING.value,
            raw_duration_seconds=4260, duration_analysis_status="long", is_vod=False,
        )
        async with self.sessions() as session:
            session.add_all([self.settings, self.download])
            await session.commit()
        self.processor = MagicMock()
        self.processor.comskip_available = True
        self.processor.ffmpeg_available = True
        self.processor.trim_buffer_tail = AsyncMock(return_value=True)
        self.processor.detect_commercials = AsyncMock(return_value=None)
        self.processor.transcode = AsyncMock(return_value=str(self.source))
        self.processor._get_duration = AsyncMock(return_value=3960)
        self.processor.get_ffmpeg_path.return_value = None
        self.processor.resolve_hw_accel.return_value = (HardwareAccel.CPU, None)
        self.patches = [
            patch("services.post_processor.post_processor", self.processor),
            patch("services.download_manager.async_session_maker", self.sessions),
            patch("services.download_manager.get_free_space_shortfall", AsyncMock(return_value=None)),
            patch("services.comskip_ini.resolve_comskip_ini", return_value=(None, False)),
            patch.object(self.manager, "_broadcast_log", AsyncMock()),
            patch.object(self.manager, "_broadcast_progress", AsyncMock()),
            patch.object(self.manager, "_update_processing", AsyncMock()),
            patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        self.folder.cleanup()

    async def _save(self):
        async with self.sessions() as session:
            await session.merge(self.download)
            await session.merge(self.settings)
            await session.commit()

    async def _process(self):
        await self._save()
        async with self.sessions() as session:
            return await self.manager._post_process(
                str(self.source), self.download.id, session, self.settings
            )

    async def _saved_outcome(self):
        async with self.sessions() as session:
            download = await session.get(Download, self.download.id)
            return download.to_dict()["buffer_trim_status"]

    async def test_success_is_persisted_before_later_processing_can_fail(self):
        self.settings.comskip_enabled = True
        self.settings.transcode_enabled = True
        self.processor.detect_commercials.side_effect = RuntimeError("commercial detection failed")
        with self.assertRaisesRegex(RuntimeError, "ComSkip failed"):
            await self._process()
        self.assertEqual(await self._saved_outcome(), "trimmed")

    async def test_saved_success_is_preserved_when_processing_restarts(self):
        await self._process()
        self.processor.trim_buffer_tail.reset_mock()
        # A later stage may already have shortened this file. A resumed job
        # must preserve the completed trim observation, not run it again.
        self.processor._get_duration.return_value = 2700
        async with self.sessions() as session:
            await self.manager._post_process(str(self.source), self.download.id, session, self.settings)
        self.processor.trim_buffer_tail.assert_not_awaited()
        self.assertEqual(await self._saved_outcome(), "trimmed")

    async def test_already_matching_file_is_not_claimed_as_a_confirmed_trim(self):
        self.processor.trim_buffer_tail.return_value = False
        await self._process()
        self.assertEqual(await self._saved_outcome(), "already_matched")

    async def test_failure_is_persisted_even_when_later_conversion_succeeds(self):
        self.settings.transcode_enabled = True
        self.processor.trim_buffer_tail.side_effect = RuntimeError("invalid candidate")
        await self._process()
        self.processor.transcode.assert_awaited_once()
        self.assertEqual(await self._saved_outcome(), "failed")

    async def test_disabled_trim_does_not_infer_success_from_a_matching_final_duration(self):
        self.settings.trim_dispatcharr_buffer = False
        self.download.recorded_duration_seconds = 3960
        await self._process()
        self.assertEqual(await self._saved_outcome(), "not_run")

    async def test_cancelled_trim_keeps_an_unconfirmed_outcome(self):
        self.processor.trim_buffer_tail.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self._process()
        self.assertEqual(await self._saved_outcome(), "in_progress")

    async def test_new_capture_resets_previous_trim_outcome(self):
        for previous in ("trimmed", "failed", "already_matched", "in_progress"):
            self.download.buffer_trim_status = previous
            self.manager._reset_duration_analysis(self.download)
            self.assertEqual(self.download.buffer_trim_status, "not_run")
            self.assertIsNone(self.download.raw_duration_seconds)
            self.assertIsNone(self.download.duration_analysis_status)

    async def test_upgrade_keeps_old_outcomes_unknown_and_is_idempotent(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE downloads DROP COLUMN buffer_trim_status"))
            await _apply_lightweight_migrations(conn)
            await _apply_lightweight_migrations(conn)
            result = await conn.execute(text("SELECT buffer_trim_status FROM downloads"))
            self.assertIsNone(result.scalar_one())

    async def test_keep_ts_uses_processing_queue_and_preserves_raw_evidence_and_padding(self):
        self.assertTrue(self.manager._needs_post_processing(self.download, self.settings))
        path, warnings = await self._process()
        self.assertEqual(path, str(self.source))
        self.assertEqual(warnings, [])
        self.processor.trim_buffer_tail.assert_awaited_once()
        self.assertEqual(self.processor.trim_buffer_tail.call_args.args, (str(self.source), 3960))
        self.processor.transcode.assert_not_awaited()
        self.processor.detect_commercials.assert_not_awaited()
        self.assertEqual(self.download.raw_duration_seconds, 4260)
        self.assertEqual(self.download.duration_analysis_status, "long")

    async def test_trim_runs_before_commercial_detection_and_conversion(self):
        self.settings.comskip_enabled = True
        self.settings.comskip_cut = False
        self.settings.transcode_enabled = True
        events = []

        async def trim(*args, **kwargs):
            events.append("trim")
            self.source.write_bytes(b"trimmed input")
            return True

        async def detect(*args, **kwargs):
            self.assertEqual(self.source.read_bytes(), b"trimmed input")
            events.append("commercial detection")

        async def convert(*args, **kwargs):
            events.append("conversion")
            return str(self.source)

        self.processor.trim_buffer_tail.side_effect = trim
        self.processor.detect_commercials.side_effect = detect
        self.processor.transcode.side_effect = convert
        await self._process()
        self.assertEqual(events, ["trim", "commercial detection", "conversion"])

    async def test_disabled_vod_and_unqualified_evidence_do_not_trim(self):
        for flag, vod, status, raw in (
            (False, False, "long", 4260),
            (True, True, "long", 4260),
            (True, False, "matched", 3960),
            (True, False, "short", 3300),
            (True, False, "unavailable", None),
            (True, False, "unreliable", 93600),
            (True, False, None, None),
            (True, False, "long", float("inf")),
        ):
            with self.subTest(flag=flag, vod=vod, status=status):
                self.settings.trim_dispatcharr_buffer = flag
                self.download.is_vod = vod
                self.download.duration_analysis_status = status
                self.download.raw_duration_seconds = raw
                await self._process()
                self.processor.trim_buffer_tail.assert_not_awaited()
        self.download.is_vod = True
        self.assertFalse(self.manager._needs_post_processing(self.download, self.settings))

    async def test_trim_failure_continues_conversion_with_original_and_warning(self):
        self.settings.transcode_enabled = True
        self.processor.trim_buffer_tail.side_effect = RuntimeError("candidate did not validate")
        _, warnings = await self._process()
        self.processor.transcode.assert_awaited_once()
        self.assertEqual(self.source.read_bytes(), b"original recording")
        self.assertEqual(warnings, ["Buffer trim failed: candidate did not validate"])

    async def test_low_space_warns_and_keeps_original_when_trim_is_only_step(self):
        with patch("services.download_manager.get_free_space_shortfall", AsyncMock(return_value=(1.0, 25))):
            _, warnings = await self._process()
        self.processor.trim_buffer_tail.assert_not_awaited()
        self.assertIn("Not enough space", warnings[0])
        self.assertEqual(self.source.read_bytes(), b"original recording")

    async def test_failed_trim_is_visible_on_completed_recording(self):
        self.processor.trim_buffer_tail.side_effect = RuntimeError("ffprobe failed")
        await self.manager._execute_post_process(self.download.id)
        async with self.sessions() as session:
            download = await session.get(Download, self.download.id)
            self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
            self.assertIn("Completed with warnings: Buffer trim failed", download.error_message)
            self.assertEqual(Path(download.output_path).read_bytes(), b"original recording")
            self.assertEqual(download.raw_duration_seconds, 4260)

    async def test_cancellation_propagates_without_running_later_steps(self):
        self.settings.transcode_enabled = True
        self.processor.trim_buffer_tail.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self._process()
        self.processor.transcode.assert_not_awaited()
        self.assertEqual(self.source.read_bytes(), b"original recording")

    async def test_legacy_processing_without_raw_evidence_is_not_trimmed(self):
        self.download.duration_analysis_status = None
        self.download.raw_duration_seconds = None
        await self._save()
        await self.manager._execute_post_process(self.download.id)
        self.processor.trim_buffer_tail.assert_not_awaited()
        async with self.sessions() as session:
            download = await session.get(Download, self.download.id)
            self.assertEqual(download.duration_analysis_status, "unavailable")
            self.assertEqual(download.status, DownloadStatus.COMPLETED.value)


class BufferTrimSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_default_and_api_roundtrip_do_not_enable_other_processing(self):
        async with self.sessions() as session:
            settings = AppSettings(transcode_enabled=False, comskip_enabled=False)
            session.add(settings)
            await session.commit()
            self.assertFalse(settings.to_dict()["trim_dispatcharr_buffer"])
            for value in (True, None, False):
                result = await update_settings(
                    SettingsUpdate(trim_dispatcharr_buffer=value), None, session
                )
                self.assertEqual(result["trim_dispatcharr_buffer"], value is not False)
                self.assertFalse(result["transcode_enabled"])
                self.assertFalse(result["comskip_enabled"])

    async def test_existing_install_migration_is_default_off_and_idempotent(self):
        async with self.sessions() as session:
            session.add(AppSettings())
            await session.commit()
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE app_settings DROP COLUMN trim_dispatcharr_buffer"))
            await _apply_lightweight_migrations(conn)
            await _apply_lightweight_migrations(conn)
            value = (await conn.execute(text("SELECT trim_dispatcharr_buffer FROM app_settings"))).scalar_one()
            self.assertEqual(value, 0)

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(trim_dispatcharr_buffer="sometimes")


if __name__ == "__main__":
    unittest.main()
