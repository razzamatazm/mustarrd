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


class CancelCleanupTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_cancel_cleans_up_partial_output_file(self):
        """Partial .ts file must be deleted when a download is cancelled mid-transfer."""
        with tempfile.TemporaryDirectory() as tmpdir:
            partial_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = partial_file
            mock_download.source_url = "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts"
            mock_download.progress = 0.0

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_download
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.commit = AsyncMock()

            @asynccontextmanager
            async def mock_session_maker():
                yield mock_session

            async def fake_download_file(url, path, download_id, session, offset=0):
                with open(path, "wb") as f:
                    f.write(b"\x00" * 1024)
                raise asyncio.CancelledError()

            with patch("services.download_manager.async_session_maker", mock_session_maker):
                with patch.object(self.manager, "_download_file", fake_download_file):
                    with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                        with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                            asyncio.run(self.manager._execute_download(1))

            self.assertFalse(
                os.path.exists(partial_file),
                "Partial download file must be deleted after cancel",
            )
            self.assertEqual(mock_download.status, DownloadStatus.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
