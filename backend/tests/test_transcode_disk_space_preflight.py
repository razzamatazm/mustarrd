"""
Regression test: post-processing must check free disk space before starting a
transcode.

Bug: downloads have a disk-space preflight, but post-processing had none.
Transcoding writes a full second copy of the recording next to the original;
when the disk was already below the configured minimum, ffmpeg would run until
ENOSPC, fail mid-write, and leave a partial output file orphaned in the
download folder.

Fix: _post_process calls disk_space.get_free_space_shortfall on the download
folder before any transcode/commercial-removal work. When free space is below
min_free_space_gb the download fails cleanly with a clear error, no ffmpeg is
started, and no partial output is written.
"""
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


class TranscodeDiskSpacePreflightTests(unittest.IsolatedAsyncioTestCase):

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

    async def _seed_db(self, min_free_space_gb: int) -> tuple[int, str]:
        ts_path = os.path.join(self.download_dir, "Show.ts")
        Path(ts_path).write_bytes(b"\x47" * 188)

        async with self.session_factory() as session:
            settings = AppSettings(
                transcode_enabled=True,
                comskip_enabled=False,
                transcode_format="mkv",
                download_folder=self.download_dir,
                completed_folder=self.completed_dir,
                delete_original_after_transcode=False,
                remux_only=False,
                min_free_space_gb=min_free_space_gb,
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

    def _make_mock_pp(self):
        mock_pp = MagicMock()
        mock_pp.comskip_available = False
        mock_pp.ffmpeg_available = True
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        mock_pp.transcode = AsyncMock(return_value="unused.mkv")
        mock_pp.detect_commercials = AsyncMock()
        mock_pp.remove_commercials = AsyncMock()
        return mock_pp

    async def test_low_disk_space_fails_before_transcode_starts(self):
        """With free space below the minimum, the download fails cleanly and ffmpeg never runs."""
        # An absurdly high minimum guarantees the preflight trips on any machine.
        download_id, ts_path = await self._seed_db(min_free_space_gb=10 ** 9)
        mock_pp = self._make_mock_pp()

        with patch("services.post_processor.post_processor", mock_pp), \
             patch("services.download_manager.async_session_maker",
                   side_effect=lambda: self.session_factory()), \
             patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
            await self.manager._execute_post_process(download_id)

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()

        self.assertEqual(
            dl.status,
            DownloadStatus.FAILED.value,
            "Post-processing must fail cleanly when disk space is below minimum.",
        )
        self.assertIn(
            "disk space",
            (dl.error_message or "").lower(),
            "The error must clearly say the failure is about disk space.",
        )
        mock_pp.transcode.assert_not_called()
        mock_pp.remove_commercials.assert_not_called()
        self.assertTrue(Path(ts_path).exists(), "Raw recording must stay in the download folder.")
        self.assertFalse(
            Path(ts_path).with_suffix(".mkv").exists(),
            "No partial transcode output may exist after the preflight failure.",
        )

    async def test_preflight_disabled_when_min_free_space_is_zero(self):
        """min_free_space_gb=0 disables the preflight and transcode proceeds."""
        download_id, ts_path = await self._seed_db(min_free_space_gb=0)
        mock_pp = self._make_mock_pp()
        transcoded = Path(ts_path).with_suffix(".mkv")

        async def fake_transcode(input_path, *args, **kwargs):
            transcoded.write_bytes(b"\x00" * 64)
            return str(transcoded)

        mock_pp.transcode = AsyncMock(side_effect=fake_transcode)

        with patch("services.post_processor.post_processor", mock_pp), \
             patch("services.download_manager.async_session_maker",
                   side_effect=lambda: self.session_factory()), \
             patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
            await self.manager._execute_post_process(download_id)

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()

        self.assertEqual(dl.status, DownloadStatus.COMPLETED.value)
        mock_pp.transcode.assert_called_once()


if __name__ == "__main__":
    unittest.main()
