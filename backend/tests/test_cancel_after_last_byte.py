"""
Regression test: cancel arriving right after the final byte must not destroy
the recording.

In _execute_download there is a window between _download_file returning the
full payload and download.status being set to COMPLETED (the transfer-complete
log broadcast and the AppSettings query are awaits). If Task.cancel() lands in
that window, the CancelledError handler previously saw the in-memory status
DOWNLOADING, marked the download CANCELLED and os.unlink'ed the fully
downloaded file:

  - Bug 1: the complete recording was deleted from disk.
  - Bug 2: the download ended CANCELLED although the data was complete.

Fix: _execute_download tracks a transfer_complete flag; when a cancel arrives
after the final byte, the handler moves the finished file to the completed
folder and finalizes the download as COMPLETED instead.
"""
import asyncio
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


class CancelAfterLastByteTests(unittest.IsolatedAsyncioTestCase):

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

    async def _seed_settings(self, download_folder: str, completed_folder: str) -> None:
        async with self.session_factory() as session:
            settings = AppSettings(
                download_folder=download_folder,
                completed_folder=completed_folder,
                transcode_enabled=False,
                comskip_enabled=False,
            )
            session.add(settings)
            await session.commit()

    async def _seed_pending_download(self, output_path: str) -> int:
        async with self.session_factory() as session:
            download = Download(
                account_id=1,
                channel_id="1",
                channel_name="Test Channel",
                program_title="Test Show",
                program_start=datetime(2024, 1, 1, 20, 0, 0),
                program_end=datetime(2024, 1, 1, 21, 0, 0),
                duration_minutes=60,
                source_url="http://provider.test/ts",
                output_path=output_path,
                status=DownloadStatus.PENDING.value,
                progress=0.0,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id

    async def _run_cancel_after_last_byte(self):
        """Drive _execute_download so a cancel lands right after the last byte.

        The fake _download_file writes the full payload and registers the
        cooperative cancel flag (as cancel_download() would). The patched
        _broadcast_log then raises CancelledError on the transfer-complete
        message -- the first await after the final byte, before the status
        flips to COMPLETED -- simulating Task.cancel() delivery.
        """
        downloads_dir = Path(self.tmpdir) / "downloads"
        downloads_dir.mkdir()
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        await self._seed_settings(str(downloads_dir), str(completed_dir))

        output_path = str(downloads_dir / "test_show.ts")
        download_id = await self._seed_pending_download(output_path)
        manager = self.manager

        async def fake_download_file(url, path, did, session, offset=0):
            Path(path).write_bytes(b"x" * 2048)
            manager._cancelled.add(did)
            return 2048

        async def cancel_on_transfer_complete(did, message, level="info"):
            if message == "Download transfer complete.":
                raise asyncio.CancelledError()

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        try:
            with patch("services.download_manager.async_session_maker", patched_session_maker), \
                 patch.object(self.manager, "_download_file", fake_download_file), \
                 patch.object(self.manager, "_broadcast_log", cancel_on_transfer_complete), \
                 patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
                 patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock):
                await self.manager._execute_download(download_id)
        except asyncio.CancelledError:
            pass

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()
        return dl, Path(output_path), completed_dir / "test_show.ts"

    async def test_cancel_after_last_byte_preserves_file(self):
        """Bug 1: the fully-downloaded file must survive a last-moment cancel."""
        dl, original_path, completed_path = await self._run_cancel_after_last_byte()

        self.assertTrue(
            completed_path.exists(),
            "The complete recording must be moved to the completed folder, not "
            "deleted, when cancel arrives right after the final byte. Before "
            "the fix the CancelledError handler os.unlink'ed the file.",
        )
        self.assertFalse(
            original_path.exists(),
            "The finished file should have been moved out of the download folder.",
        )

    async def test_cancel_after_last_byte_finalizes_completed(self):
        """Bug 2: the download must end COMPLETED, not CANCELLED."""
        dl, _original_path, completed_path = await self._run_cancel_after_last_byte()

        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "Download must end COMPLETED when cancel arrives after the final "
            "byte. Before the fix the handler overwrote it with CANCELLED.",
        )
        self.assertEqual(dl.output_path, str(completed_path))
        self.assertNotIn(
            dl.id,
            self.manager._cancelled,
            "The stale cancel flag must be cleared so the finalized recording "
            "cannot be deleted by a later queue pass.",
        )


if __name__ == "__main__":
    unittest.main()
