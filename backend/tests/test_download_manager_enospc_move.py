import asyncio
import errno
import os
import shutil
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


def _make_session(download):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = download
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()
    return session


class ENOSPCMoveTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_enospc_on_move_preserves_source_file(self):
        """Source .ts must NOT be deleted when shutil.move raises ENOSPC."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = source_file
            mock_download.source_url = "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts"
            mock_download.progress = 0.0
            mock_download.completed_at = None
            mock_download.error_message = None
            mock_download.total_size = None

            session = _make_session(mock_download)

            @asynccontextmanager
            async def mock_session_maker():
                yield session

            async def fake_download_file(url, path, download_id, sess, offset=0):
                with open(path, "wb") as f:
                    f.write(b"\x00" * 1024)
                return 1024

            def raise_enospc(path, completed_folder, download_folder=None):
                raise OSError(errno.ENOSPC, "No space left on device")

            with patch("services.download_manager.async_session_maker", mock_session_maker):
                with patch.object(self.manager, "_download_file", fake_download_file):
                    with patch.object(self.manager, "_move_to_completed", raise_enospc):
                        with patch.object(self.manager, "_broadcast_progress", AsyncMock()):
                            with patch.object(self.manager, "_broadcast_log", AsyncMock()):
                                with patch.object(self.manager, "_sync_schedule_status", AsyncMock()):
                                    with patch.object(self.manager, "_needs_post_processing", return_value=False):
                                        asyncio.run(self.manager._execute_download(1))

            self.assertTrue(
                os.path.exists(source_file),
                "Completed source file must NOT be deleted when move to completed folder fails with ENOSPC",
            )
            self.assertEqual(mock_download.status, DownloadStatus.FAILED.value)

    def test_move_to_completed_cleans_partial_dest_on_enospc(self):
        """Partial file at dest must be removed when shutil.move raises ENOSPC."""
        with tempfile.TemporaryDirectory() as src_dir:
            with tempfile.TemporaryDirectory() as dst_dir:
                source_file = os.path.join(src_dir, "show.ts")
                dest_file = os.path.join(dst_dir, "show.ts")

                with open(source_file, "wb") as f:
                    f.write(b"\x00" * 1024)

                # Simulate a partial copy at dest before ENOSPC
                with open(dest_file, "wb") as f:
                    f.write(b"\x00" * 512)

                original_move = shutil.move

                def fake_move(src, dst):
                    # Partial copy already exists at dst; now raise ENOSPC
                    raise OSError(errno.ENOSPC, "No space left on device")

                with patch("shutil.move", fake_move):
                    with self.assertRaises(OSError):
                        self.manager._move_to_completed(source_file, dst_dir)

                self.assertFalse(
                    os.path.exists(dest_file),
                    "Partial file at dest must be cleaned up after ENOSPC in _move_to_completed",
                )
                self.assertTrue(
                    os.path.exists(source_file),
                    "Source file must still exist after failed move",
                )


if __name__ == "__main__":
    unittest.main()
