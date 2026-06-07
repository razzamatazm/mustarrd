"""
Tests that permanently failed downloads get completed_at stamped.

The failure badge counts downloads where completed_at >= last visit.
If a failure path forgets to stamp completed_at, the badge silently undercounts.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus


class DownloadManagerCompletedAtTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def _make_session(self, download):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = download
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def ctx():
            yield mock_session

        return ctx

    def test_execute_download_failure_stamps_completed_at(self):
        """Network error during download must stamp completed_at on the FAILED record.

        Without this, the failure badge cannot count the download because the
        query filters on completed_at IS NOT NULL.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_path
            mock_download.source_url = "http://provider/timeshift/user/pass/60/2026-01-01:00-00/1.ts"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 0
            mock_download.completed_at = None

            async def fail_download(url, path, download_id, session):
                raise Exception("Connection reset by peer")

            with patch("services.download_manager.async_session_maker", self._make_session(mock_download)):
                with patch.object(self.manager, "_download_file", fail_download):
                    with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                        with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                            asyncio.run(self.manager._execute_download(1))

            self.assertEqual(mock_download.status, DownloadStatus.FAILED.value)
            self.assertIsNotNone(
                mock_download.completed_at,
                "FAILED download must have completed_at stamped so the failure badge can count it",
            )

    def test_execute_post_process_failure_stamps_completed_at(self):
        """Post-processing error must stamp completed_at on the FAILED record.

        Without this, a download that completes the transfer but fails FFmpeg
        is invisible to the failure badge.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "show.ts")
            with open(input_path, "wb") as f:
                f.write(b"\x00" * 1024)

            mock_download = MagicMock()
            mock_download.id = 2
            mock_download.status = DownloadStatus.PROCESSING.value
            mock_download.output_path = input_path
            mock_download.progress = 0.0
            mock_download.completed_at = None

            with patch("services.download_manager.async_session_maker", self._make_session(mock_download)):
                with patch.object(self.manager, "_needs_post_processing", return_value=True):
                    with patch.object(self.manager, "_post_process", side_effect=Exception("FFmpeg exit 1")):
                        with patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir):
                            with patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir):
                                with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                                    with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                                        asyncio.run(self.manager._execute_post_process(2))

            self.assertEqual(mock_download.status, DownloadStatus.FAILED.value)
            self.assertIsNotNone(
                mock_download.completed_at,
                "Post-processing failure must stamp completed_at so the failure badge counts it",
            )


if __name__ == "__main__":
    unittest.main()
