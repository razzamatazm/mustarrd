"""
Regression test: cancelling a PROCESSING download that is queued but not yet started
in post-processing must delete the source file from the download folder.

When a download completes the download phase, _execute_download sets status=PROCESSING
and puts the download_id in _post_queue, then returns. If cancel_download() is called
before process_post_queue picks up the job, the id is not in _active_post so no
task.cancel() fires. cancel_download writes CANCELLED to the DB and returns True.

Later, process_post_queue picks up the id, finds it in _cancelled, discards it, and
continues; _execute_post_process is never started. No file cleanup occurs.

Contrast: _execute_download's CancelledError handler explicitly calls
os.unlink(download.output_path) when a download is cancelled mid-flight.
The post-processing cancel path has no equivalent.

Result: the source .ts sits in the download folder indefinitely, invisible to the app
(status=CANCELLED), silently consuming disk space. On Unraid installs with limited
storage this can fill the drive without any user-visible indication.

Expected fix: cancel_download or the process_post_queue skip path should delete
download.output_path when cancelling a queued-not-started post-processing job.
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
from models import Download, DownloadStatus
from services.download_manager import DownloadManager


class PostProcessQueuedCancelCleanupTests(unittest.IsolatedAsyncioTestCase):

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
                progress=100.0,
            )
            session.add(dl)
            await session.commit()
            await session.refresh(dl)
            return dl.id

    async def test_cancel_queued_post_process_deletes_source_file(self):
        """
        Cancelling a PROCESSING download not yet in _active_post must clean up the source file.

        This test FAILS on the current codebase: cancel_download returns True and the
        post_queue skip path discards the job without deleting the .ts file.
        """
        source_ts = Path(self.tmpdir) / "show.ts"
        source_ts.write_bytes(b"fake ts content")

        download_id = await self._seed_processing_download(str(source_ts))

        # Put the id in _post_queue as if _execute_download just completed.
        await self.manager._post_queue.put(download_id)

        # The download is NOT in _active_post (post-processing has not started).
        assert download_id not in self.manager._active_post

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        with patch("services.download_manager.async_session_maker", patched_session_maker), \
             patch.object(self.manager, "_sync_schedule_status", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
            result = await self.manager.cancel_download(download_id)

            self.assertTrue(result, "cancel_download must return True for a PROCESSING download.")

            # Run the real process_post_queue inside the patch context so
            # _delete_cancelled_post_file reaches the test's in-memory DB.
            task = asyncio.create_task(self.manager.process_post_queue())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # This assertion FAILS on the current codebase.
        # The source .ts remains on disk because no cleanup runs when the
        # queued post-processing job is skipped due to cancellation.
        self.assertFalse(
            source_ts.exists(),
            "Source .ts file must be deleted when a queued-not-started post-processing "
            "job is cancelled. Currently the file is left on disk indefinitely.",
        )


if __name__ == "__main__":
    unittest.main()
