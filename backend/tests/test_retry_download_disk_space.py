"""
Regression: POST /api/downloads/{id}/retry bypasses the min_free_space_gb check.

POST /api/downloads calls check_disk_space() and returns HTTP 507 when disk is
below min_free_space_gb.  The retry endpoint calls download_manager.retry_download()
directly without any disk-space guard.  A user whose download failed due to full
disk can immediately retry — the download restarts and hits the same ENOSPC
condition.

These tests show the gap and currently FAIL.  They should pass once the fix
(calling check_disk_space in the retry endpoint) lands.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _run(coro):
    return asyncio.run(coro)


class TestRetryDownloadDiskSpaceCheck(unittest.TestCase):
    """retry_download endpoint must check disk space before re-queuing."""

    def test_create_download_calls_check_disk_space(self):
        """
        Baseline: the create-download handler calls check_disk_space.
        Verifies that check_disk_space is importable and invoked on the create path.
        """
        import api.downloads as dl_module

        # check_disk_space IS imported and used in create_download.
        self.assertTrue(
            hasattr(dl_module, "check_disk_space"),
            "check_disk_space must be imported in api.downloads for create_download",
        )

        # Verify it is called from create_download by inspecting the source.
        import inspect
        source = inspect.getsource(dl_module.create_download)
        self.assertIn(
            "check_disk_space",
            source,
            "create_download must call check_disk_space",
        )

    def test_retry_download_calls_check_disk_space(self):
        """
        The retry endpoint must call check_disk_space — just as create_download does.

        Currently FAILS because the retry handler does not call check_disk_space.
        A fix should add: `await check_disk_space(session)` before re-queuing.
        """
        import inspect
        from api.downloads import retry_download

        source = inspect.getsource(retry_download)
        self.assertIn(
            "check_disk_space",
            source,
            "retry_download must call check_disk_space to enforce min_free_space_gb, "
            "just as create_download does. Currently the retry path has no disk-space "
            "guard, so retrying a download that failed due to full disk immediately "
            "restarts it without checking whether enough space is now available.",
        )

    def test_retry_endpoint_returns_507_when_disk_low(self):
        """
        When disk is below min_free_space_gb, the retry endpoint must return HTTP 507.

        Currently FAILS because no disk check is performed during retry.
        """
        from fastapi import HTTPException
        from auth import AuthContext
        from api.downloads import retry_download
        from models import Download, DownloadStatus

        auth = AuthContext(
            authenticated=True,
            is_admin=True,
            user=None,
            user_id=None,
            provider="admin_local",
        )

        download = MagicMock(spec=Download)
        download.id = 42
        download.status = DownloadStatus.FAILED.value

        # Session returns the failed download, then app settings with high threshold.
        from models import AppSettings
        app_settings_obj = MagicMock(spec=AppSettings)
        app_settings_obj.download_folder = "/tmp"
        app_settings_obj.min_free_space_gb = 999  # impossibly high threshold

        call_count = [0]

        async def _fake_execute(_query):
            r = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                r.scalar_one_or_none.return_value = download
            else:
                r.scalar_one_or_none.return_value = app_settings_obj
            return r

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=_fake_execute)
        session.refresh = AsyncMock()

        with patch("api.downloads.download_manager.retry_download",
                   new_callable=AsyncMock, return_value=True), \
             patch("services.disk_space.os.path.exists", return_value=True), \
             patch("services.disk_space.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            usage = MagicMock()
            usage.free = 5 * 1024 ** 3  # 5 GB free, far below 999 GB min
            mock_thread.return_value = usage

            with self.assertRaises(HTTPException) as ctx:
                _run(retry_download(download_id=42, auth=auth, session=session))

            self.assertEqual(
                ctx.exception.status_code, 507,
                "retry_download must return HTTP 507 when disk is below "
                "min_free_space_gb. Currently no disk check is performed.",
            )


if __name__ == "__main__":
    unittest.main()
