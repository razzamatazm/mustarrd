"""
Regression test: exception after _move_to_completed deletes the completed recording.

In _execute_download, after _move_to_completed succeeds, download.output_path is
updated in the Python object to point to the completed folder. If any exception
fires between that update and the final session.commit() (e.g., in
_sync_schedule_status, or if the commit itself fails due to SQLite busy / ENOSPC /
NAS mount going offline), the except Exception handler at line ~729:

  1. Re-queries the same SQLAlchemy session and gets the same Python Download
     object (identity map) with output_path already pointing to completed_path.
  2. Calls os.unlink(download.output_path), permanently deleting the completed
     recording from the completed folder.

The CancelledError handler already guards against this correctly by checking
download.status == COMPLETED before any deletion. The generic except Exception
handler has no equivalent guard.

Fix needed: in the except Exception handler, skip os.unlink when output_path
is already inside the completed folder, mirroring the CancelledError logic.
"""
import contextlib
import os
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

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

from database import Base
from models import Download, DownloadStatus
from services.download_manager import DownloadManager


class ExceptionAfterMoveTests(unittest.IsolatedAsyncioTestCase):

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

    async def test_exception_after_move_preserves_completed_file(self):
        """
        An exception after _move_to_completed must not delete the completed
        recording.

        This test FAILS before the fix: the except Exception handler calls
        os.unlink on completed_path because download.output_path was updated
        in the Python object before the commit, and the identity map returns
        the same object in the exception handler.
        """
        download_path = str(Path(self.tmpdir) / "test_show.ts")
        completed_dir = Path(self.tmpdir) / "completed"
        completed_dir.mkdir()
        completed_path = str(completed_dir / "test_show.ts")
        # Simulate the file already residing in the completed folder after a
        # successful _move_to_completed call.
        Path(completed_path).touch()

        download_id = await self._seed_pending_download(download_path)

        # Raise a generic Exception from _sync_schedule_status only on the first
        # call (the success-path call at line ~683, after _move_to_completed).
        # The second call comes from the error handler itself; let it succeed so
        # we reach the os.unlink at line ~744 and can observe the deletion.
        sync_calls = [0]

        async def raise_on_first_sync(session, did, status):
            sync_calls[0] += 1
            if sync_calls[0] == 1:
                raise Exception("Simulated transient DB error after file move")

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
                patch.object(self.manager, "_sync_schedule_status", side_effect=raise_on_first_sync),
                patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
                patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
                patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock),
            ):
                await self.manager._execute_download(download_id)
        except Exception:
            pass

        # The completed file must still exist after the failure.
        # BEFORE the fix this assertion fails because the except Exception handler
        # calls os.unlink(completed_path) via the identity-mapped download object.
        self.assertTrue(
            Path(completed_path).exists(),
            "Completed file must NOT be deleted when an exception fires after "
            "_move_to_completed. The except Exception handler must skip os.unlink "
            "when output_path already points into the completed folder, matching "
            "the guard already present in the CancelledError handler.",
        )


if __name__ == "__main__":
    unittest.main()
