"""
Regression test: a failed DB commit after _move_to_completed must not lose the
completed recording.

In _execute_download the success path moves the file to the completed folder
and then commits the COMPLETED state. If that commit fails (SQLite busy, disk
I/O error, NAS going offline), SQLAlchemy leaves the session pending rollback.
The except Exception handler then re-queried through the same session, which
raised PendingRollbackError, so neither COMPLETED nor FAILED was ever
persisted: the row stayed DOWNLOADING forever and a restart re-downloaded (or
failed) a recording whose file was already safely in the completed folder.

Fix: the handler keys off the local moved-completed path and persists the
COMPLETED state through _finalize_completed_after_interrupt, which rolls back
the broken session and retries on a fresh one. The completed file is never
deleted.
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
from models import Download, DownloadStatus
from services.download_manager import DownloadManager


class CommitFailureAfterMoveTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_commit_failure_after_move_keeps_completed_recording(self):
        """The recording must end COMPLETED (file intact) when the final commit fails.

        The hooked _sync_schedule_status (the last await before the final
        commit on the success path) sets a NOT NULL column to None, so the
        real session.commit() fails with IntegrityError and leaves the session
        in the pending-rollback state -- exactly what a real failed commit
        does. Before the fix the except handler crashed on the unusable
        session and the row stayed DOWNLOADING.
        """
        download_path = str(Path(self.tmpdir) / "test_show.ts")
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        completed_path = str(completed_dir / "test_show.ts")
        # Simulate the file already residing in the completed folder after a
        # successful _move_to_completed call.
        Path(completed_path).touch()

        download_id = await self._seed_pending_download(download_path)

        sync_calls = [0]

        async def poison_first_commit(session, did, status):
            sync_calls[0] += 1
            if sync_calls[0] == 1:
                obj = await session.get(Download, did)
                # NOT NULL column: the success-path commit right after this
                # call fails with IntegrityError.
                obj.source_url = None

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                yield real_session

        try:
            with (
                patch("services.download_manager.async_session_maker", patched_session_maker),
                patch.object(self.manager, "_download_file", new_callable=AsyncMock, return_value=1024),
                patch.object(self.manager, "_needs_post_processing", return_value=False),
                patch.object(self.manager, "_move_to_completed", return_value=completed_path),
                patch.object(self.manager, "_sync_schedule_status", side_effect=poison_first_commit),
                patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
                patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
                patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock),
            ):
                await self.manager._execute_download(download_id)
        except Exception:
            pass

        self.assertTrue(
            Path(completed_path).exists(),
            "A DB commit failure must never delete a recording that was "
            "already moved to the completed folder.",
        )

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            dl = result.scalar_one()

        self.assertEqual(
            dl.status,
            DownloadStatus.COMPLETED.value,
            "Download must be persisted as COMPLETED even when the original "
            "commit fails and poisons the session. Before the fix the row "
            "stayed DOWNLOADING and the recording was re-downloaded or failed "
            "on restart.",
        )
        self.assertEqual(
            dl.output_path,
            completed_path,
            "output_path must point at the completed file so the app can find it.",
        )


if __name__ == "__main__":
    unittest.main()
