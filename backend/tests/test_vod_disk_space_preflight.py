import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from api.vod import _check_disk_space


def _make_session(download_folder="/tmp/downloads", min_free_space_gb=25):
    app_settings = MagicMock()
    app_settings.download_folder = download_folder
    app_settings.min_free_space_gb = min_free_space_gb

    scalar_mock = MagicMock()
    scalar_mock.scalar_one_or_none = MagicMock(return_value=app_settings)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar_mock)
    return session


def _make_disk_usage(free_bytes):
    usage = MagicMock()
    usage.free = free_bytes
    return usage


class VodDiskSpacePreflightTests(unittest.IsolatedAsyncioTestCase):

    async def test_raises_507_when_below_threshold(self):
        """download_movie and download_series must return 507 when free space < min_free_space_gb."""
        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=10 * 1024 ** 3)  # 10 GB free, threshold 25 GB

        with patch("api.vod.shutil.disk_usage", return_value=usage), \
             patch("api.vod.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await _check_disk_space(session)

        self.assertEqual(ctx.exception.status_code, 507)
        self.assertIn("disk space", ctx.exception.detail.lower())
        self.assertIn("10.0 GB free", ctx.exception.detail)
        self.assertIn("25 GB required", ctx.exception.detail)

    async def test_passes_when_sufficient_space(self):
        """No exception raised when free space >= min_free_space_gb."""
        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=50 * 1024 ** 3)  # 50 GB free

        with patch("api.vod.shutil.disk_usage", return_value=usage), \
             patch("api.vod.os.path.exists", return_value=True):
            await _check_disk_space(session)  # must not raise

    async def test_passes_at_exact_threshold(self):
        """Exactly min_free_space_gb free must not block the download."""
        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=25 * 1024 ** 3)

        with patch("api.vod.shutil.disk_usage", return_value=usage), \
             patch("api.vod.os.path.exists", return_value=True):
            await _check_disk_space(session)  # must not raise

    async def test_skips_check_when_folder_missing(self):
        """If the download folder does not exist yet, the check is skipped (no error)."""
        session = _make_session(min_free_space_gb=25)

        with patch("api.vod.os.path.exists", return_value=False):
            await _check_disk_space(session)  # must not raise

    async def test_uses_default_min_free_when_not_configured(self):
        """When min_free_space_gb is None in settings, default of 25 GB is used."""
        app_settings = MagicMock()
        app_settings.download_folder = "/tmp/downloads"
        app_settings.min_free_space_gb = None
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=app_settings)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=scalar_mock)

        usage = _make_disk_usage(free_bytes=10 * 1024 ** 3)  # 10 GB < default 25 GB

        with patch("api.vod.shutil.disk_usage", return_value=usage), \
             patch("api.vod.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await _check_disk_space(session)

        self.assertEqual(ctx.exception.status_code, 507)


if __name__ == "__main__":
    unittest.main()
