"""
Regression test: restart recovery must not skip post-processing when the
download folder and the completed folder are configured to the same path.

recover_incomplete_downloads treats "the file is under the completed folder"
as proof that post-processing already finished and marks the download
COMPLETED. When both folders point at the same directory, the raw downloaded
.ts file sits "under the completed folder" before any Comskip/transcode work
has run, so a restart mid-processing silently finalized the unprocessed file.

Fix: the completed-folder shortcut is only trusted when the two folders are
distinct; with equal folders the download is requeued for post-processing.
"""
import contextlib
import shutil
import sys
import tempfile
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


class RecoverEqualFoldersPostProcessTests(unittest.IsolatedAsyncioTestCase):

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

    async def _seed(self, download_folder: str, completed_folder: str,
                    output_path: str, transcode_enabled: bool) -> int:
        async with self.session_factory() as session:
            session.add(AppSettings(
                download_folder=download_folder,
                completed_folder=completed_folder,
                transcode_enabled=transcode_enabled,
                comskip_enabled=False,
            ))
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

    async def _recover(self) -> None:
        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        with (
            patch("services.download_manager.async_session_maker", patched_session_maker),
            patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
            patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
        ):
            await self.manager.recover_incomplete_downloads()

    async def _get_download(self, download_id: int) -> Download:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            return result.scalar_one()

    async def test_equal_folders_processing_requeues_post_processing(self):
        """Equal folders + post-processing enabled: requeue, don't finalize."""
        media_dir = Path(self.tmpdir) / "media"
        media_dir.mkdir()
        raw_path = media_dir / "show.ts"
        raw_path.write_bytes(b"x" * 1024)

        download_id = await self._seed(
            download_folder=str(media_dir),
            completed_folder=str(media_dir),
            output_path=str(raw_path),
            transcode_enabled=True,
        )

        await self._recover()

        dl = await self._get_download(download_id)
        self.assertEqual(
            dl.status,
            DownloadStatus.PROCESSING.value,
            "With download folder == completed folder, a PROCESSING download "
            "must be requeued for post-processing on restart. Before the fix "
            "the raw .ts was 'under the completed folder' and got marked "
            "COMPLETED without Comskip/transcode ever running.",
        )
        self.assertEqual(
            self.manager._post_queue.qsize(), 1,
            "The download must be put back on the post-processing queue.",
        )
        self.assertEqual(self.manager._post_queue.get_nowait(), download_id)

    async def test_equal_folders_without_post_processing_marks_completed(self):
        """Equal folders + post-processing disabled: finalizing is correct."""
        media_dir = Path(self.tmpdir) / "media"
        media_dir.mkdir()
        raw_path = media_dir / "show.ts"
        raw_path.write_bytes(b"x" * 1024)

        download_id = await self._seed(
            download_folder=str(media_dir),
            completed_folder=str(media_dir),
            output_path=str(raw_path),
            transcode_enabled=False,
        )

        await self._recover()

        dl = await self._get_download(download_id)
        self.assertEqual(dl.status, DownloadStatus.COMPLETED.value)
        self.assertEqual(dl.output_path, str(raw_path))
        self.assertTrue(raw_path.exists(), "Move-to-completed must be a no-op.")
        self.assertEqual(self.manager._post_queue.qsize(), 0)

    async def test_distinct_folders_file_in_completed_still_finalizes(self):
        """Distinct folders: a file already in completed is still finalized."""
        downloads_dir = Path(self.tmpdir) / "downloads"
        downloads_dir.mkdir()
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        moved_path = completed_dir / "show.mkv"
        moved_path.write_bytes(b"x" * 1024)

        download_id = await self._seed(
            download_folder=str(downloads_dir),
            completed_folder=str(completed_dir),
            output_path=str(moved_path),
            transcode_enabled=True,
        )

        await self._recover()

        dl = await self._get_download(download_id)
        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "With distinct folders, a PROCESSING download whose file already "
            "sits in the completed folder must still be finalized as COMPLETED.",
        )
        self.assertEqual(self.manager._post_queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
