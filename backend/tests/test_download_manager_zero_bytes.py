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


class ZeroBytesDownloadTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_zero_byte_response_marked_failed_not_completed(self):
        """A provider returning an empty body must be marked FAILED, not COMPLETED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_file
            mock_download.source_url = "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 0

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_download
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.commit = AsyncMock()

            @asynccontextmanager
            async def mock_session_maker():
                yield mock_session

            async def fake_download_file_zero_bytes(url, path, download_id, session, offset=0):
                # Provider returns HTTP 200 with empty body: file created but no bytes written.
                open(path, "wb").close()
                return 0

            with patch("services.download_manager.async_session_maker", mock_session_maker):
                with patch.object(self.manager, "_download_file", fake_download_file_zero_bytes):
                    with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                        with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                            asyncio.run(self.manager._execute_download(1))

            self.assertEqual(
                mock_download.status,
                DownloadStatus.FAILED.value,
                "Zero-byte download must be marked FAILED, not COMPLETED",
            )
            self.assertFalse(
                os.path.exists(output_file),
                "Zero-byte output file must be cleaned up after failure",
            )
            self.assertIn(
                "empty response",
                mock_download.error_message.lower(),
                "Error message must mention empty response",
            )

    def test_normal_download_not_affected(self):
        """A download with actual bytes must still be marked COMPLETED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")
            completed_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 2
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_file
            mock_download.source_url = "http://provider/timeshift/user/pass/60/2026-06-06:00-00/2.ts"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 0

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_download
            mock_settings_result = MagicMock()
            mock_settings_result.scalar_one_or_none.return_value = None
            mock_session = AsyncMock()
            mock_session.commit = AsyncMock()

            call_count = 0

            async def side_effect_execute(stmt):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return mock_result
                return mock_settings_result

            mock_session.execute = side_effect_execute

            @asynccontextmanager
            async def mock_session_maker():
                yield mock_session

            async def fake_download_file_with_bytes(url, path, download_id, session, offset=0):
                with open(path, "wb") as f:
                    f.write(b"\x00" * 1024)
                return 1024

            with patch("services.download_manager.async_session_maker", mock_session_maker):
                with patch.object(self.manager, "_download_file", fake_download_file_with_bytes):
                    with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                        with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                            with patch.object(self.manager, "_needs_post_processing", return_value=False):
                                with patch.object(self.manager, "_move_to_completed", return_value=completed_file):
                                    with patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir):
                                        with patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir):
                                            with patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()):
                                                asyncio.run(self.manager._execute_download(2))

            self.assertEqual(
                mock_download.status,
                DownloadStatus.COMPLETED.value,
                "Normal download with bytes must be marked COMPLETED",
            )


if __name__ == "__main__":
    unittest.main()
