import asyncio
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager
from models import DownloadStatus


class _FakeDownload:
    def __init__(self, dl_id, output_path):
        self.id = dl_id
        self.status = DownloadStatus.PENDING.value
        self.output_path = output_path
        self.source_url = "http://provider.test/timeshift/1.ts"
        self.requested_by_user_id = None
        self.is_vod = False
        self.progress = 0.0
        self.downloaded_bytes = 0
        self.file_size = 0
        self.error_message = None
        self.completed_at = None
        self.account_id = 1


class _FakeSession:
    """Minimal async session stub. Returns the same download for every execute()."""

    def __init__(self, download):
        self._download = download

    async def execute(self, _stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._download
        return result

    async def commit(self):
        pass

    def add(self, obj):
        pass

    async def refresh(self, obj):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _make_session_ctx(session):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class DiskFullTests(unittest.IsolatedAsyncioTestCase):

    async def test_enospc_partial_output_file_cleaned_up(self):
        """
        When a download fails with ENOSPC (disk full), the partial output file
        must be deleted so the disk is freed and recovery is possible.

        BUG: This test currently FAILS because _execute_download does not remove
        the partial file in its except-Exception handler. The partial file persists
        on disk, keeps the disk full, and blocks every subsequent download until
        the operator manually deletes it.

        Fix: in the except-Exception block of _execute_download, unlink
        download.output_path if it exists before marking FAILED.
        """
        manager = DownloadManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "Show.S01E01.ts")
            download = _FakeDownload(dl_id=42, output_path=output_path)
            session = _FakeSession(download)

            async def fake_download_file(url, path, dl_id, ses):
                # Simulate provider sends some bytes, then disk fills.
                with open(path, "wb") as fh:
                    fh.write(b"\x47" * 512 * 1024)  # 512 KB partial TS data
                raise OSError(errno.ENOSPC, "No space left on device")

            with patch.object(manager, "_download_file", new=fake_download_file), \
                 patch("services.download_manager.async_session_maker",
                       return_value=_make_session_ctx(session)), \
                 patch.object(manager, "_broadcast_progress", new_callable=AsyncMock), \
                 patch.object(manager, "_broadcast_log", new_callable=AsyncMock):
                await manager._execute_download(42)

            # Assertions must be inside the tempdir context so the directory
            # is not cleaned up before we check file existence.

            # Download correctly marked FAILED.
            self.assertEqual(
                download.status,
                DownloadStatus.FAILED.value,
                "Download must be marked FAILED on ENOSPC.",
            )
            self.assertIn(
                "disk space",
                (download.error_message or "").lower(),
                "error_message must mention disk space.",
            )

            # Partial file must be gone. This assertion FAILS on the current codebase.
            self.assertFalse(
                os.path.exists(output_path),
                "Partial download file must be deleted on ENOSPC to free disk space. "
                "Currently _execute_download leaves it on disk, keeping the disk full "
                "and blocking all subsequent downloads until manual cleanup.",
            )


if __name__ == "__main__":
    unittest.main()
