"""DownloadManager passes the scheduled length and remux-fallback status through.

The post-processor uses the scheduled recording length to sanity-check ffprobe
durations, and reports when a failed remux falls back to a full re-encode. The
download manager has to hand the length down and finish with a log line that
names what actually happened ("Re-encode complete", not "Remux complete").
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import AppSettings
from services.download_manager import DownloadManager


class PostProcessStatusPlumbingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.logs = []

        async def capture_log(download_id, message):
            self.logs.append(message)

        self.manager._broadcast_log = capture_log

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _mock_post_processor(self):
        mock_pp = MagicMock()
        mock_pp.comskip_available = False
        mock_pp.ffmpeg_available = True
        mock_pp.resolve_hw_accel = lambda accel: (accel, None)
        mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
        return mock_pp

    async def test_remux_fallback_logs_reencode_complete(self):
        async with self.session_maker() as session:
            settings = AppSettings(
                transcode_enabled=True,
                remux_only=True,
                transcode_format="mkv",
                min_free_space_gb=0,
            )
            session.add(settings)
            await session.commit()

            mock_pp = self._mock_post_processor()

            async def fake_transcode(*args, status_callback=None, **kwargs):
                await status_callback("Remux failed; re-encoding with CPU (libx264).")
                return "/tmp/recording.mkv"

            mock_pp.transcode = AsyncMock(side_effect=fake_transcode)

            with patch("services.post_processor.post_processor", mock_pp):
                final_path, warnings = await self.manager._post_process(
                    "/tmp/recording.ts",
                    download_id=1,
                    session=session,
                    settings=settings,
                    expected_duration_seconds=3900,
                )

        self.assertEqual(final_path, "/tmp/recording.mkv")
        self.assertEqual(warnings, [])
        kwargs = mock_pp.transcode.await_args.kwargs
        self.assertEqual(kwargs["expected_duration_seconds"], 3900)
        self.assertIn("Re-encode complete: /tmp/recording.mkv", self.logs)
        self.assertNotIn("Remux complete: /tmp/recording.mkv", self.logs)

    async def test_plain_remux_still_logs_remux_complete(self):
        async with self.session_maker() as session:
            settings = AppSettings(
                transcode_enabled=True,
                remux_only=True,
                transcode_format="mkv",
                min_free_space_gb=0,
            )
            session.add(settings)
            await session.commit()

            mock_pp = self._mock_post_processor()
            mock_pp.transcode = AsyncMock(return_value="/tmp/recording.mkv")

            with patch("services.post_processor.post_processor", mock_pp):
                await self.manager._post_process(
                    "/tmp/recording.ts",
                    download_id=1,
                    session=session,
                    settings=settings,
                )

        self.assertIn("Remux complete: /tmp/recording.mkv", self.logs)
        self.assertIsNone(mock_pp.transcode.await_args.kwargs["expected_duration_seconds"])


if __name__ == "__main__":
    unittest.main()
