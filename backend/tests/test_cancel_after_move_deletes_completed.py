"""
Regression test: CancelledError after _move_to_completed must not delete the
completed recording or mark the download CANCELLED.

Success path in _execute_download: _move_to_completed moves the file to the
completed folder, download.output_path is updated to that path, and
download.status is set to COMPLETED in memory. The first await after that is
_sync_schedule_status. If Task.cancel() arrives there, the CancelledError
handler previously overwrote the status with CANCELLED and called
os.unlink(output_path) -- deleting the completed file from the completed folder
permanently.

Fix: in the except asyncio.CancelledError handler, if download.status is already
COMPLETED in memory, commit the completed state and return without touching the
file.
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


class CancelledAfterMoveTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_cancel_after_move_preserves_completed_status_and_file(self):
        """Cancel after move must leave status COMPLETED and not delete the file."""
        download_path = str(Path(self.tmpdir) / "test_show.ts")
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        completed_path = str(completed_dir / "test_show.ts")
        # Pre-create the completed file to represent the post-move state.
        Path(completed_path).touch()

        download_id = await self._seed_pending_download(download_path)

        # _sync_schedule_status raises CancelledError on the first call (the
        # success-path call at line 683), then returns normally on the second
        # call (from within the CancelledError handler after our fix).
        sync_calls = [0]

        async def hooked_sync_schedule_status(session, did, status):
            sync_calls[0] += 1
            if sync_calls[0] == 1:
                raise asyncio.CancelledError()

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        try:
            with patch("services.download_manager.async_session_maker", patched_session_maker), \
                 patch.object(self.manager, "_download_file", new_callable=AsyncMock, return_value=1024), \
                 patch.object(self.manager, "_needs_post_processing", return_value=False), \
                 patch.object(self.manager, "_move_to_completed", return_value=completed_path), \
                 patch.object(self.manager, "_sync_schedule_status", side_effect=hooked_sync_schedule_status), \
                 patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
                 patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock), \
                 patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock):
                await self.manager._execute_download(download_id)
        except (asyncio.CancelledError, Exception):
            pass

        async with self.session_factory() as session:
            result = await session.execute(select(Download).where(Download.id == download_id))
            dl = result.scalar_one()

        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "Download must be COMPLETED after CancelledError fires post-move. "
            "Before the fix, the handler overwrote it with CANCELLED.",
        )
        self.assertTrue(
            Path(completed_path).exists(),
            "Completed file must not be deleted when cancel arrives after the move. "
            "Before the fix, the handler called os.unlink on the completed path.",
        )


if __name__ == "__main__":
    unittest.main()
