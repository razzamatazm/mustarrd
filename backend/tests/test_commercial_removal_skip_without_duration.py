"""When ffprobe cannot measure a recording, commercial removal is skipped.

Cutting needs the real length of the file to place the last segment boundary.
Without it (ffprobe missing, timed out, or unreadable output) the recording is
kept uncut, a warning names the reason, and the rest of post-processing (the
remux/transcode to the chosen format) still runs. Before this, the download
failed outright.
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
from services.post_processor import CommercialRemovalSkipped


class CommercialRemovalSkipWithoutDurationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_skipped_cut_keeps_recording_and_still_transcodes(self):
        async with self.session_maker() as session:
            settings = AppSettings(
                comskip_enabled=True,
                transcode_enabled=True,
                remux_only=False,
                transcode_format="mkv",
                min_free_space_gb=0,
            )
            session.add(settings)
            await session.commit()

            mock_pp = MagicMock()
            mock_pp.comskip_available = True
            mock_pp.ffmpeg_available = True
            mock_pp.resolve_hw_accel = lambda accel: (accel, None)
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.detect_commercials = AsyncMock(return_value="/tmp/recording.edl")
            mock_pp.remove_commercials = AsyncMock(
                side_effect=CommercialRemovalSkipped(
                    "Commercial removal skipped: ffprobe could not measure the recording."
                )
            )
            mock_pp.transcode = AsyncMock(return_value="/tmp/recording.mkv")

            with patch("services.post_processor.post_processor", mock_pp):
                final_path, warnings = await self.manager._post_process(
                    "/tmp/recording.ts",
                    download_id=1,
                    session=session,
                    settings=settings,
                    expected_duration_seconds=3600,
                )

        mock_pp.transcode.assert_awaited_once()
        self.assertEqual(mock_pp.transcode.await_args.args[0], "/tmp/recording.ts")
        self.assertEqual(final_path, "/tmp/recording.mkv")
        self.assertEqual(len(warnings), 1)
        self.assertIn("ffprobe", warnings[0])
        self.assertTrue(any("Commercial removal skipped" in line for line in self.logs))
        self.assertFalse(any("Commercial removal complete" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
