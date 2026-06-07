"""
Regression test for ComSkip exception fallthrough bug.

When ComSkip or the commercial removal step raises an exception, the
download_manager was swallowing it and continuing to a plain transcode.
The user received an MKV with commercials intact and no indication that
anything had gone wrong.

Fix: the exception is re-raised so _execute_post_process marks the
download as FAILED with a clear message instead of silently completing.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import Base
from models import AppSettings, Download, DownloadStatus
from services.download_manager import DownloadManager


class ComSkipExceptionFallthroughTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _make_settings(self, session):
        settings = AppSettings(
            comskip_enabled=True,
            transcode_enabled=True,
            remux_only=False,
            transcode_format="mkv",
        )
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
        return settings

    async def test_comskip_exception_propagates_not_swallowed(self):
        """detect_commercials raising must propagate, not fall through to plain transcode."""
        async with self.session_maker() as session:
            settings = await self._make_settings(session)

            logs = []

            async def log_cb(msg):
                logs.append(msg)

            mock_pp = MagicMock()
            mock_pp.comskip_available = True
            mock_pp.ffmpeg_available = True
            mock_pp.get_comskip_path.return_value = "/usr/bin/comskip"
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.detect_commercials = AsyncMock(
                side_effect=RuntimeError("ComSkip timed out after 300s")
            )
            mock_pp.transcode = AsyncMock(return_value="/tmp/output.mkv")
            mock_pp.remove_commercials = AsyncMock(return_value="/tmp/output.mkv")

            with patch("services.post_processor.post_processor", mock_pp):
                with self.assertRaises(RuntimeError):
                    await self.manager._post_process(
                        "/tmp/recording.ts",
                        download_id=1,
                        session=session,
                        settings=settings,
                    )

            # transcode must NOT have been called: we should have raised, not silently produced output
            mock_pp.transcode.assert_not_called()
            mock_pp.remove_commercials.assert_not_called()

    async def test_remove_commercials_exception_propagates(self):
        """remove_commercials raising must also propagate, not produce a commercials-included file."""
        async with self.session_maker() as session:
            settings = await self._make_settings(session)

            mock_pp = MagicMock()
            mock_pp.comskip_available = True
            mock_pp.ffmpeg_available = True
            mock_pp.get_comskip_path.return_value = "/usr/bin/comskip"
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.detect_commercials = AsyncMock(return_value="/tmp/recording.edl")
            mock_pp.remove_commercials = AsyncMock(
                side_effect=RuntimeError("FFmpeg killed after 600s")
            )
            mock_pp.transcode = AsyncMock(return_value="/tmp/output.mkv")

            with patch("services.post_processor.post_processor", mock_pp):
                with self.assertRaises(RuntimeError):
                    await self.manager._post_process(
                        "/tmp/recording.ts",
                        download_id=1,
                        session=session,
                        settings=settings,
                    )

            mock_pp.transcode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
