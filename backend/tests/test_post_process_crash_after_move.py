"""
Regression test: a post-processing crash after _move_to_completed must not
mark the download FAILED.

In _execute_post_process, once the output file has been moved to the completed
folder, any exception (working-file cleanup, schedule sync, the final DB
commit) previously fell into the generic except Exception handler, which
marked the download FAILED with output_path already pointing at the completed
file. The recording looked failed even though the file was fine -- and a retry
of that "failed" download would have streamed straight over the completed file
(then deleted it if the retry failed).

Fix: _execute_post_process tracks the moved path and finalizes the download as
COMPLETED (with a warning in error_message) when the file already survived in
the completed folder.
"""
import contextlib
import sys
import tempfile
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database import Base
from models import AppSettings, Download, DownloadStatus
from services.download_manager import DownloadManager


class PostProcessCrashAfterMoveTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.tmpdir = tempfile.mkdtemp()

    async def asyncTearDown(self):
        await self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _seed_processing_download(self, output_path: str) -> int:
        async with self.session_factory() as session:
            dl = Download(
                account_id=1,
                channel_id="1",
                channel_name="Test Channel",
                program_title="Test Show",
                program_start=datetime(2024, 1, 1, 20, 0, 0),
                program_end=datetime(2024, 1, 1, 21, 0, 0),
                duration_minutes=60,
                source_url="http://provider.test/ts",
                output_path=output_path,
                status=DownloadStatus.PROCESSING.value,
                progress=50.0,
            )
            session.add(dl)
            await session.commit()
            await session.refresh(dl)
            return dl.id

    async def test_crash_after_move_finalizes_completed_not_failed(self):
        """Crash between the move and the final commit must end COMPLETED."""
        downloads_dir = Path(self.tmpdir) / "downloads"
        downloads_dir.mkdir()
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()

        source_path = downloads_dir / "show.ts"
        source_path.write_bytes(b"x" * 1024)
        completed_path = completed_dir / "show.ts"

        async with self.session_factory() as session:
            session.add(AppSettings(
                download_folder=str(downloads_dir),
                completed_folder=str(completed_dir),
                transcode_enabled=True,
                comskip_enabled=False,
            ))
            await session.commit()

        download_id = await self._seed_processing_download(str(source_path))

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        try:
            with (
                patch("services.download_manager.async_session_maker", patched_session_maker),
                patch.object(self.manager, "_post_process", new_callable=AsyncMock,
                             return_value=(str(source_path), [])),
                # First call after the real _move_to_completed: simulate a crash
                # in the post-move stretch of the success path.
                patch.object(self.manager, "_cleanup_working_files",
                             side_effect=RuntimeError("simulated crash after move")),
                patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
                patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
                patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock),
            ):
                await self.manager._execute_post_process(download_id)
        except Exception:
            pass

        self.assertTrue(
            completed_path.exists(),
            "The moved file must still exist in the completed folder.",
        )

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()

        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "A post-processing crash after the file reached the completed "
            "folder must finalize the download as COMPLETED. Before the fix "
            "it was marked FAILED with output_path pointing at the completed "
            "file, so a retry would overwrite or delete the good recording.",
        )
        self.assertEqual(
            dl.output_path,
            str(completed_path),
            "output_path must point at the surviving completed file.",
        )
        self.assertIn(
            "Completed with warnings",
            dl.error_message or "",
            "The post-move error should be surfaced as a warning, not a failure.",
        )


if __name__ == "__main__":
    unittest.main()
