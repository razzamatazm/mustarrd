"""
Regression test: partial transcode output (.mkv/.mp4) orphaned on warning-path failure.

When FFmpeg fails during transcode (comskip_enabled=False, transcode_enabled=True),
the exception is caught as a WARNING at download_manager.py:1196 and the download is
marked COMPLETED.  _cleanup_working_files IS called (success path), but its patterns
do not include *.mkv / *.mp4, so the half-written output file is never deleted and
accumulates in the download working folder.

Fix needed: after _post_process returns with transcode warnings, remove the partial
transcode output file ({stem}.mkv / {stem}.mp4) before moving the original to completed.
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

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

from database import Base
from models import AppSettings, Download, DownloadStatus
from services.download_manager import DownloadManager


class PartialTranscodeOutputOrphanTests(unittest.IsolatedAsyncioTestCase):

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

    async def _seed_db(self):
        """Insert AppSettings + a PROCESSING Download; return (download_id, ts_path)."""
        ts_path = os.path.join(self.download_dir, "Show.ts")
        Path(ts_path).write_bytes(b"\x47" * 188)  # minimal valid TS

        async with self.session_factory() as session:
            settings = AppSettings(
                transcode_enabled=True,
                comskip_enabled=False,
                transcode_format="mkv",
                download_folder=self.download_dir,
                completed_folder=self.completed_dir,
                delete_original_after_transcode=False,
                remux_only=True,
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

    async def test_partial_mkv_cleaned_up_when_transcode_fails_as_warning(self):
        """
        After FFmpeg fails mid-transcode, the partial .mkv must be deleted even
        though the download completes successfully (with a warning).

        BUG: _cleanup_working_files patterns omit *.mkv / *.mp4, so the partial
        output persists in the working download folder indefinitely.

        This test FAILS on the current codebase.
        """
        download_id, ts_path = await self._seed_db()
        mkv_path = Path(ts_path).with_suffix(".mkv")

        async def fake_transcode(input_path, *args, **kwargs):
            # Simulate FFmpeg writing a partial file then failing.
            mkv_path.write_bytes(b"\x00" * 512)
            raise RuntimeError("FFmpeg failed: invalid data found when processing input")

        mock_pp = MagicMock()
        mock_pp.comskip_available = False
        mock_pp.ffmpeg_available = True
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        mock_pp.transcode = AsyncMock(side_effect=fake_transcode)

        with patch("services.post_processor.post_processor", mock_pp), \
             patch("services.download_manager.async_session_maker",
                   side_effect=lambda: self.session_factory()), \
             patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
            await self.manager._execute_post_process(download_id)

        # Download must be COMPLETED with a transcode warning.
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one_or_none()

        self.assertIsNotNone(dl, "Download record must still exist.")
        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "A failed transcode must produce COMPLETED (with warnings), not FAILED.",
        )
        self.assertIn(
            "Transcode failed",
            dl.error_message or "",
            "error_message must record the transcode warning.",
        )

        # The partial .mkv must be gone.  This FAILS on the current codebase because
        # _cleanup_working_files does not match *.mkv in its pattern list.
        self.assertFalse(
            mkv_path.exists(),
            "Partial .mkv from a failed transcode must be deleted. "
            "Currently _cleanup_working_files omits *.mkv, leaving the file to "
            "accumulate in the download working folder across repeated failures.",
        )


if __name__ == "__main__":
    unittest.main()
