"""
Regression test for misleading ComSkip failure error message.

When ComSkip fails during post-processing, the error used to say
"Recording not saved to avoid producing a file with commercials intact."
This was false: the raw .ts file is still in the download folder,
not deleted. Operators were re-downloading unnecessarily, sometimes
overwriting the still-intact original.

Fix: the error message now says the raw recording is still in the
download folder.
"""
import os
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


class ComSkipFailureErrorMessageTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.tmpdir = tempfile.mkdtemp()

    async def asyncTearDown(self):
        await self.engine.dispose()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _seed_db(self, ts_path: str) -> int:
        async with self.session_factory() as session:
            settings = AppSettings(
                comskip_enabled=True,
                transcode_enabled=True,
                remux_only=False,
                download_folder=self.tmpdir,
                completed_folder=self.tmpdir,
            )
            session.add(settings)
            await session.flush()

            download = Download(
                account_id=1,
                channel_id="1",
                channel_name="Test Channel",
                program_title="Test Show",
                program_start=datetime(2024, 1, 1, 20, 0, 0),
                program_end=datetime(2024, 1, 1, 21, 0, 0),
                duration_minutes=60,
                source_url="http://provider.test/ts",
                output_path=ts_path,
                status=DownloadStatus.PROCESSING.value,
                progress=0.0,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id

    def _make_mock_pp(self):
        mock_pp = MagicMock()
        mock_pp.comskip_available = True
        mock_pp.ffmpeg_available = False
        mock_pp.get_comskip_path.return_value = "/usr/bin/comskip"
        mock_pp.detect_commercials = AsyncMock(
            side_effect=RuntimeError("Comskip exited with code 1")
        )
        mock_pp.remove_commercials = AsyncMock()
        mock_pp.transcode = AsyncMock()
        return mock_pp

    async def _run_post_process(self, download_id: int):
        with patch("services.download_manager.async_session_maker",
                   side_effect=lambda: self.session_factory()), \
             patch("services.post_processor.post_processor", self._make_mock_pp()), \
             patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock), \
             patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock):
            await self.manager._execute_post_process(download_id)

    async def test_comskip_failure_error_mentions_download_folder_not_deleted(self):
        """download.error_message must say file is in download folder, not that it was not saved."""
        ts_path = os.path.join(self.tmpdir, "show.ts")
        Path(ts_path).write_bytes(b"\x47" * 188)

        download_id = await self._seed_db(ts_path)
        await self._run_post_process(download_id)

        async with self.session_factory() as session:
            result = await session.execute(select(Download).where(Download.id == download_id))
            dl = result.scalar_one()

        self.assertEqual(dl.status, DownloadStatus.FAILED.value)
        self.assertIsNotNone(dl.error_message)
        self.assertNotIn(
            "not saved",
            dl.error_message,
            "Error must not say 'not saved': the raw .ts is still on disk",
        )
        self.assertIn(
            "download folder",
            dl.error_message,
            "Error must mention the download folder so operators know where to find the file",
        )

    async def test_comskip_failure_raw_file_still_present_on_disk(self):
        """The .ts file must remain on disk after ComSkip failure, not be deleted."""
        ts_path = os.path.join(self.tmpdir, "show.ts")
        Path(ts_path).write_bytes(b"\x47" * 188)

        download_id = await self._seed_db(ts_path)
        await self._run_post_process(download_id)

        self.assertTrue(
            os.path.exists(ts_path),
            "Raw .ts must still exist on disk after ComSkip failure",
        )


if __name__ == "__main__":
    unittest.main()
