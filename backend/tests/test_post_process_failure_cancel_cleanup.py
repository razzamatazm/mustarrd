"""
Regression test: Comskip artifacts and partial transcode outputs must be
cleaned up when a post-processing task fails or is cancelled.

Bug: _execute_post_process only called _cleanup_working_files on the success
path. When post-processing raised (e.g. "ComSkip failed: ...") or the task was
cancelled mid-run, all working files created so far — .edl/.txt sidecars,
_seg*.ts extraction temps, and the partially-written .mkv/.mp4 transcode
output — were orphaned in the download folder forever.

Fix: the CancelledError and Exception handlers now call
_cleanup_working_files with delete_original=False, so the temp files are
removed while the raw recording stays in the download folder (failures keep
the .log/ffmpeg logs for debugging).
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from models import AppSettings, Download, DownloadStatus
from services.download_manager import DownloadManager


class PostProcessFailureCancelCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.download_dir = tempfile.mkdtemp()
        self.completed_dir = tempfile.mkdtemp()

    async def asyncTearDown(self):
        await self.engine.dispose()
        shutil.rmtree(self.download_dir, ignore_errors=True)
        shutil.rmtree(self.completed_dir, ignore_errors=True)

    async def _seed_db(self) -> tuple[int, str]:
        ts_path = os.path.join(self.download_dir, "Show.ts")
        Path(ts_path).write_bytes(b"\x47" * 188)

        async with self.session_factory() as session:
            settings = AppSettings(
                comskip_enabled=True,
                transcode_enabled=True,
                transcode_format="mkv",
                remux_only=False,
                download_folder=self.download_dir,
                completed_folder=self.completed_dir,
                delete_original_after_transcode=False,
                min_free_space_gb=0,
            )
            session.add(settings)
            await session.flush()

            download = Download(
                account_id=1,
                channel_id="100",
                channel_name="Test Channel",
                program_title="Show",
                program_start=datetime(2024, 1, 1, 20, 0, 0),
                program_end=datetime(2024, 1, 1, 21, 0, 0),
                duration_minutes=60,
                source_url="http://provider.test/timeshift/1.ts",
                output_path=ts_path,
                status=DownloadStatus.PROCESSING.value,
                progress=0.0,
                is_vod=False,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id, ts_path

    def _run_with_patches(self, mock_pp):
        return [
            patch("services.post_processor.post_processor", mock_pp),
            patch("services.download_manager.async_session_maker",
                  side_effect=lambda: self.session_factory()),
            patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
            patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
        ]

    async def _get_download(self, download_id):
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            return result.scalar_one_or_none()

    async def test_failure_cleans_comskip_artifacts_and_partial_output(self):
        """A ComSkip pipeline failure must not orphan working files."""
        download_id, ts_path = await self._seed_db()
        ts_file = Path(ts_path)
        edl = ts_file.with_suffix(".edl")
        txt = ts_file.with_suffix(".txt")
        log = ts_file.with_suffix(".log")
        partial_mkv = ts_file.with_suffix(".mkv")
        seg = ts_file.with_stem("Show_seg0")

        async def fake_detect(*args, **kwargs):
            edl.write_text("60.0 120.0 0\n")
            txt.write_text("comskip output")
            log.write_text("comskip log")
            return str(edl)

        async def fake_remove_commercials(*args, **kwargs):
            seg.write_bytes(b"\x47" * 188)
            partial_mkv.write_bytes(b"\x00" * 512)
            raise RuntimeError("ffmpeg concat failed")

        mock_pp = MagicMock()
        mock_pp.comskip_available = True
        mock_pp.ffmpeg_available = True
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        mock_pp.detect_commercials = AsyncMock(side_effect=fake_detect)
        mock_pp.remove_commercials = AsyncMock(side_effect=fake_remove_commercials)
        mock_pp.transcode = AsyncMock()

        p = self._run_with_patches(mock_pp)
        with p[0], p[1], p[2], p[3]:
            await self.manager._execute_post_process(download_id)

        dl = await self._get_download(download_id)
        self.assertEqual(dl.status, DownloadStatus.FAILED.value)
        self.assertTrue(ts_file.exists(), "Raw recording must stay in the download folder.")
        self.assertFalse(edl.exists(), ".edl must be cleaned up after failure.")
        self.assertFalse(txt.exists(), ".txt must be cleaned up after failure.")
        self.assertFalse(seg.exists(), "_seg*.ts must be cleaned up after failure.")
        self.assertFalse(partial_mkv.exists(), "Partial .mkv must be cleaned up after failure.")
        self.assertTrue(log.exists(), ".log is kept on failure for debugging.")

    async def test_cancel_cleans_comskip_artifacts_and_partial_output(self):
        """Cancelling mid post-processing must not orphan working files."""
        download_id, ts_path = await self._seed_db()
        ts_file = Path(ts_path)
        edl = ts_file.with_suffix(".edl")
        txt = ts_file.with_suffix(".txt")
        partial_mkv = ts_file.with_suffix(".mkv")

        async def fake_detect(*args, **kwargs):
            edl.write_text("60.0 120.0 0\n")
            txt.write_text("comskip output")
            partial_mkv.write_bytes(b"\x00" * 512)
            raise asyncio.CancelledError()

        mock_pp = MagicMock()
        mock_pp.comskip_available = True
        mock_pp.ffmpeg_available = True
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        mock_pp.detect_commercials = AsyncMock(side_effect=fake_detect)
        mock_pp.remove_commercials = AsyncMock()
        mock_pp.transcode = AsyncMock()

        p = self._run_with_patches(mock_pp)
        with p[0], p[1], p[2], p[3]:
            try:
                await self.manager._execute_post_process(download_id)
            except asyncio.CancelledError:
                pass

        dl = await self._get_download(download_id)
        self.assertEqual(dl.status, DownloadStatus.CANCELLED.value)
        self.assertTrue(ts_file.exists(), "Raw recording must stay in the download folder.")
        self.assertFalse(edl.exists(), ".edl must be cleaned up after cancel.")
        self.assertFalse(txt.exists(), ".txt must be cleaned up after cancel.")
        self.assertFalse(partial_mkv.exists(), "Partial .mkv must be cleaned up after cancel.")

    async def test_cancel_after_cancel_download_cleans_sidecars(self):
        """Regression: cancel_download pre-writes CANCELLED to DB before the asyncio
        CancelledError fires. The status guard in the CancelledError handler would see
        CANCELLED and skip cleanup, orphaning EDL and comskip sidecar files."""
        download_id, ts_path = await self._seed_db()
        ts_file = Path(ts_path)
        edl = ts_file.with_suffix(".edl")
        txt = ts_file.with_suffix(".txt")
        log = ts_file.with_suffix(".log")
        partial_mkv = ts_file.with_suffix(".mkv")

        session_factory = self.session_factory

        async def fake_detect(*args, **kwargs):
            edl.write_text("60.0 120.0 0\n")
            txt.write_text("comskip output")
            log.write_text("comskip log")
            return str(edl)

        async def fake_remove(*args, **kwargs):
            partial_mkv.write_bytes(b"\x00" * 512)
            # Simulate what cancel_download does: write CANCELLED to DB before
            # the CancelledError reaches _execute_post_process.
            async with session_factory() as session:
                result = await session.execute(
                    select(Download).where(Download.id == download_id)
                )
                dl = result.scalar_one_or_none()
                if dl:
                    dl.status = DownloadStatus.CANCELLED.value
                    await session.commit()
            raise asyncio.CancelledError()

        mock_pp = MagicMock()
        mock_pp.comskip_available = True
        mock_pp.ffmpeg_available = True
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        mock_pp.detect_commercials = AsyncMock(side_effect=fake_detect)
        mock_pp.remove_commercials = AsyncMock(side_effect=fake_remove)
        mock_pp.transcode = AsyncMock()

        p = self._run_with_patches(mock_pp)
        with p[0], p[1], p[2], p[3]:
            try:
                await self.manager._execute_post_process(download_id)
            except asyncio.CancelledError:
                pass

        dl = await self._get_download(download_id)
        self.assertEqual(dl.status, DownloadStatus.CANCELLED.value)
        self.assertTrue(ts_file.exists(), "Raw recording must stay in the download folder.")
        self.assertFalse(edl.exists(), ".edl must be cleaned up even when DB already shows CANCELLED.")
        self.assertFalse(txt.exists(), ".txt must be cleaned up even when DB already shows CANCELLED.")
        self.assertFalse(partial_mkv.exists(), "Partial .mkv must be cleaned up.")
        self.assertFalse(log.exists(), ".log must be cleaned up on cancel (keep_logs=False).")


if __name__ == "__main__":
    unittest.main()
