"""
Regression test: CancelledError after _move_to_completed in _execute_post_process must
commit COMPLETED so the row does not stay stuck as PROCESSING.

In _execute_post_process, after _post_process() returns successfully, the flow is:
  1. _select_final_path()       (sync)
  2. _move_to_completed()       (sync, file is now in completed folder)
  3. _cleanup_working_files()   (sync)
  4. download.status = COMPLETED (sync, only in memory, not yet committed)
  5. await _sync_schedule_status(...)   <-- FIRST await after the move
  6. await session.commit()

If task.cancel() fires at step 5 or 6, CancelledError is raised. The handler
re-selects the download using the same session: SQLAlchemy's identity map returns
the in-memory object where status is already COMPLETED, so the handler skips the
CANCELLED branch and does not commit. When the session context exits, the uncommitted
COMPLETED is rolled back and the row stays PROCESSING. The file is in the completed
folder but the DB says PROCESSING: the recording is effectively orphaned.

This mirrors the download-phase bug covered by test_cancel_after_move_deletes_completed.py
(TODO 1512804545061982270), but for the post-processing phase which has no equivalent guard.

Expected fix: the CancelledError handler should detect the file is already in the
completed folder (or output_path was updated) and commit COMPLETED instead.
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


class PostProcessCancelAfterMoveTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_cancel_after_post_process_move_preserves_completed_status(self):
        """CancelledError after _move_to_completed must commit COMPLETED, not leave row as PROCESSING."""
        source_path = str(Path(self.tmpdir) / "show.ts")
        Path(source_path).touch()
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        completed_path = str(completed_dir / "show.mkv")
        Path(completed_path).touch()

        download_id = await self._seed_processing_download(source_path)

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
                 patch.object(self.manager, "_needs_post_processing", return_value=True), \
                 patch.object(self.manager, "_post_process", new_callable=AsyncMock,
                              return_value=(completed_path, [])), \
                 patch.object(self.manager, "_select_final_path", return_value=completed_path), \
                 patch.object(self.manager, "_move_to_completed", return_value=completed_path), \
                 patch.object(self.manager, "_cleanup_working_files"), \
                 patch.object(self.manager, "_sync_schedule_status",
                              side_effect=hooked_sync_schedule_status), \
                 patch.object(self.manager, "_resolve_completed_folder",
                              return_value=str(completed_dir)), \
                 patch.object(self.manager, "_resolve_download_folder",
                              return_value=self.tmpdir), \
                 patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
                 patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock), \
                 patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock):
                await self.manager._execute_post_process(download_id)
        except asyncio.CancelledError:
            pass

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()

        # This assertion FAILS on the current codebase.
        # The handler re-selects via the same session (identity map returns COMPLETED
        # in memory), skips the CANCELLED branch, and does not commit. The session
        # exits with a rollback, leaving the row stuck as PROCESSING.
        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "Post-processing download must be COMPLETED after CancelledError fires "
            "after _move_to_completed. Currently the uncommitted COMPLETED is rolled "
            "back on session exit, leaving the row stuck as PROCESSING.",
        )


if __name__ == "__main__":
    unittest.main()
