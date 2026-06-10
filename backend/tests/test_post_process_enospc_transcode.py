"""
Regression test: transcoded output must survive when _move_to_completed fails.

Bug: transcode() deletes the original .ts inside the function before returning.
If _move_to_completed_async then raises OSError (ENOSPC on a cross-filesystem
NAS copy), the error handler called _cleanup_working_files with the .ts path as
the completed_path guard.  The glob matched the .mkv, saw it was not the .ts,
and deleted it.  Both files gone; download lost.

Fix: _execute_post_process now tracks post_processed_path (the transcode output)
before calling _move_to_completed_async.  Error handlers pass
completed_output_path or post_processed_path or working_input_path as the guard,
so the intact .mkv is skipped by the cleanup glob.
"""
import asyncio
import errno
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


class PostProcessENOSPCTranscodeTests(unittest.IsolatedAsyncioTestCase):

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

    async def _seed_db(self, ts_path: str) -> int:
        async with self.session_factory() as session:
            settings = AppSettings(
                transcode_enabled=True,
                comskip_enabled=False,
                transcode_format="mkv",
                remux_only=False,
                download_folder=self.download_dir,
                completed_folder=self.completed_dir,
                delete_original_after_transcode=True,
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
            return download.id

    async def test_transcoded_mkv_survives_enospc_on_move(self):
        """
        When transcode succeeds (deleting .ts) but _move_to_completed raises
        ENOSPC, the .mkv in the download folder must NOT be deleted.
        Download must be marked FAILED with the .mkv still recoverable.
        """
        ts_path = os.path.join(self.download_dir, "Show.ts")
        mkv_path = os.path.join(self.download_dir, "Show.mkv")
        Path(ts_path).write_bytes(b"\x47" * 188)

        download_id = await self._seed_db(ts_path)

        async def fake_post_process(file_path, download_id, session, settings):
            # Simulate transcode: delete .ts, create .mkv (delete_original_after_transcode=True)
            Path(file_path).unlink(missing_ok=True)
            Path(mkv_path).write_bytes(b"\x00" * 1024)
            return mkv_path, []

        async def raise_enospc(path, completed_folder, download_folder=None):
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("services.download_manager.async_session_maker",
                   side_effect=lambda: self.session_factory()):
            with patch.object(self.manager, "_post_process", fake_post_process):
                with patch.object(self.manager, "_move_to_completed_async", raise_enospc):
                    with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                        with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                            with patch.object(self.manager, "_sync_schedule_status", AsyncMock()):
                                await self.manager._execute_post_process(download_id)

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one_or_none()

        self.assertEqual(dl.status, DownloadStatus.FAILED.value,
                         "Download must be FAILED when move raises ENOSPC.")
        self.assertFalse(os.path.exists(ts_path),
                         "Original .ts was deleted by transcode; should remain absent.")
        self.assertTrue(os.path.exists(mkv_path),
                         "Transcoded .mkv must NOT be deleted when move to completed fails.")


if __name__ == "__main__":
    unittest.main()
