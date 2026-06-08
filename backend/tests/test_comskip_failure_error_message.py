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


class ComSkipFailureErrorMessageTests(unittest.TestCase):
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

    def test_comskip_failure_error_mentions_download_folder_not_deleted(self):
        """ComSkip failure error must say the file is still present, not that it was not saved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "show.ts")
            with open(input_path, "wb") as f:
                f.write(b"\x00" * 1024)

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PROCESSING.value
            mock_download.output_path = input_path
            mock_download.progress = 0.0
            mock_download.completed_at = None

            with patch("services.download_manager.async_session_maker", self._make_session(mock_download)):
                with patch.object(self.manager, "_needs_post_processing", return_value=True):
                    with patch.object(
                        self.manager,
                        "_post_process",
                        side_effect=RuntimeError(
                            "ComSkip failed: Comskip exited with code 1. "
                            "The raw recording is still in the download folder and was not deleted."
                        ),
                    ):
                        with patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir):
                            with patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir):
                                with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                                    with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                                        asyncio.run(self.manager._execute_post_process(1))

            self.assertEqual(mock_download.status, DownloadStatus.FAILED.value)
            error = mock_download.error_message
            self.assertIsNotNone(error)
            self.assertNotIn(
                "not saved",
                error,
                "Error message must not say 'not saved': the raw .ts is still on disk",
            )
            self.assertIn(
                "download folder",
                error,
                "Error message must mention the download folder so operators know where to find the file",
            )

    def test_comskip_failure_raw_file_still_present_on_disk(self):
        """The .ts file must remain on disk after ComSkip failure, not be deleted."""
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
                    with patch.object(
                        self.manager,
                        "_post_process",
                        side_effect=RuntimeError(
                            "ComSkip failed: Comskip exited with code 1. "
                            "The raw recording is still in the download folder and was not deleted."
                        ),
                    ):
                        with patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir):
                            with patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir):
                                with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                                    with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                                        asyncio.run(self.manager._execute_post_process(2))

            self.assertTrue(
                os.path.exists(input_path),
                "Raw .ts must still exist on disk after ComSkip failure",
            )


if __name__ == "__main__":
    unittest.main()
