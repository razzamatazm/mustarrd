"""
Regression test: CancelledError before the first DB read leaves download stuck PENDING.

When asyncio.CancelledError fires during the initial session.execute() in
_execute_download — before `download = result.scalar_one_or_none()` runs —
the variable `download` is unbound. The except asyncio.CancelledError handler
immediately accessed download.status, raising NameError. Python does not fall
through to except Exception, so the NameError propagated as an unhandled task
exception. The download record stayed PENDING with no active task, recoverable
only by manual retry or restart.

Fix: initialize `download = None` before the try block; in the CancelledError
handler, re-fetch the record from DB when download is still None, then guard
all attribute access with `if download:`.
"""
import asyncio
import contextlib
import sys
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


class CancelledBeforeDownloadFetchedTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_pending_download(self) -> int:
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
                output_path="/tmp/test_cancelled_before_fetch.ts",
                status=DownloadStatus.PENDING.value,
                progress=0.0,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id

    async def test_cancelled_before_first_db_read_marks_download_cancelled(self):
        """CancelledError before download is fetched must mark it CANCELLED, not leave it PENDING."""
        download_id = await self._seed_pending_download()

        @contextlib.asynccontextmanager
        async def patched_session_maker():
            async with self.session_factory() as real_session:
                calls = [0]
                orig_execute = real_session.execute

                async def hooked_execute(*args, **kwargs):
                    calls[0] += 1
                    if calls[0] == 1:
                        raise asyncio.CancelledError()
                    return await orig_execute(*args, **kwargs)

                real_session.execute = hooked_execute
                yield real_session

        try:
            with patch("services.download_manager.async_session_maker", patched_session_maker), \
                 patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
                 patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
                await self.manager._execute_download(download_id)
        except (NameError, asyncio.CancelledError):
            pass  # NameError here is the pre-fix symptom; test will fail the assertion below

        async with self.session_factory() as session:
            result = await session.execute(select(Download).where(Download.id == download_id))
            dl = result.scalar_one()

        self.assertEqual(
            dl.status,
            DownloadStatus.CANCELLED.value,
            "Download must be CANCELLED after CancelledError fires before first DB read. "
            "Before the fix, a NameError left it stuck PENDING.",
        )


if __name__ == "__main__":
    unittest.main()
