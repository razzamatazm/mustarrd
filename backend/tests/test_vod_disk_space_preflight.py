"""
Regression tests: VOD download endpoints must check disk space after request
validation, not before.

Before fix:
  - download_series called _check_disk_space before build_episode_download, so
    a request with a nonexistent account_id or an empty episodes list would
    return 507 Insufficient Storage instead of 404 / an empty result.
  - The disk-space logic was duplicated verbatim from downloads.py, risking drift.

After fix:
  - check_disk_space lives in services/disk_space.py and is shared by all
    endpoints.
  - download_series runs the check inside the episode loop, just before
    queue_download, so validation errors and no-op batches are handled first.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException


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


def _make_auth():
    auth = MagicMock()
    auth.user_id = 1
    auth.provider = "admin_local"
    auth.is_admin = True
    return auth


class CheckDiskSpaceUnitTests(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the shared check_disk_space helper."""

    async def test_raises_507_when_below_threshold(self):
        """507 raised when free space < min_free_space_gb."""
        from services.disk_space import check_disk_space

        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=10 * 1024 ** 3)

        with patch("services.disk_space.shutil.disk_usage", return_value=usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await check_disk_space(session)

        self.assertEqual(ctx.exception.status_code, 507)
        self.assertIn("disk space", ctx.exception.detail.lower())
        self.assertIn("10.0 GB free", ctx.exception.detail)
        self.assertIn("25 GB required", ctx.exception.detail)

    async def test_passes_when_sufficient_space(self):
        """No exception raised when free space >= min_free_space_gb."""
        from services.disk_space import check_disk_space

        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=50 * 1024 ** 3)

        with patch("services.disk_space.shutil.disk_usage", return_value=usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            await check_disk_space(session)

    async def test_passes_at_exact_threshold(self):
        """Exactly min_free_space_gb free must not block the download."""
        from services.disk_space import check_disk_space

        session = _make_session(min_free_space_gb=25)
        usage = _make_disk_usage(free_bytes=25 * 1024 ** 3)

        with patch("services.disk_space.shutil.disk_usage", return_value=usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            await check_disk_space(session)

    async def test_skips_check_when_folder_missing(self):
        """If the download folder does not exist, the check is skipped."""
        from services.disk_space import check_disk_space

        session = _make_session(min_free_space_gb=25)

        with patch("services.disk_space.os.path.exists", return_value=False):
            await check_disk_space(session)

    async def test_uses_default_min_free_when_not_configured(self):
        """When min_free_space_gb is None, the default of 25 GB is used."""
        from services.disk_space import check_disk_space

        app_settings = MagicMock()
        app_settings.download_folder = "/tmp/downloads"
        app_settings.min_free_space_gb = None
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=app_settings)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=scalar_mock)

        usage = _make_disk_usage(free_bytes=10 * 1024 ** 3)

        with patch("services.disk_space.shutil.disk_usage", return_value=usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await check_disk_space(session)

        self.assertEqual(ctx.exception.status_code, 507)


class SeriesDownloadOrderingTests(unittest.IsolatedAsyncioTestCase):
    """Endpoint-level tests: disk-space check runs after validation, not before."""

    async def test_bad_account_returns_404_not_507(self):
        """download_series with a nonexistent account_id must return 404, not 507.

        Before fix: disk-space check ran before build_episode_download, so a
        nonexistent account under low disk returned 507 instead of 404.
        After fix: build_episode_download runs first; ValueError -> 404 before
        the disk check is ever reached.
        """
        from api.vod import download_series, SeriesDownloadRequest, EpisodeItem

        data = SeriesDownloadRequest(
            account_id=999,
            series_id="s1",
            series_name="Test Show",
            episodes=[EpisodeItem(id="e1", season=1, episode_num=1)],
        )
        disk_usage = _make_disk_usage(free_bytes=5 * 1024 ** 3)

        with patch("api.vod.build_episode_download",
                   new=AsyncMock(side_effect=ValueError("Account not found"))), \
             patch("services.disk_space.shutil.disk_usage", return_value=disk_usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await download_series(
                    data=data, auth=_make_auth(), session=_make_session()
                )

        self.assertEqual(ctx.exception.status_code, 404,
                         "Expected 404 for bad account_id, not 507 from premature disk check")

    async def test_empty_episodes_returns_empty_not_507(self):
        """download_series with an empty episodes list returns empty, not 507.

        Before fix: disk-space check ran before the loop; an empty batch under
        low disk returned 507 even though no download would start.
        After fix: the loop body is never entered, so the check never runs and
        an empty result is returned normally.
        """
        from api.vod import download_series, SeriesDownloadRequest

        data = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Test Show",
            episodes=[],
        )
        disk_usage = _make_disk_usage(free_bytes=5 * 1024 ** 3)

        with patch("services.disk_space.shutil.disk_usage", return_value=disk_usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            result = await download_series(
                data=data, auth=_make_auth(), session=_make_session()
            )

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["downloads"], [])

    async def test_low_disk_blocks_series_after_validation(self):
        """download_series raises 507 when disk is low and the account is valid.

        Verifies the disk check still runs; it just runs after build_episode_download
        succeeds.
        """
        from api.vod import download_series, SeriesDownloadRequest, EpisodeItem

        data = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Test Show",
            episodes=[EpisodeItem(id="e1", season=1, episode_num=1)],
        )
        fake_download = MagicMock()
        disk_usage = _make_disk_usage(free_bytes=5 * 1024 ** 3)

        with patch("api.vod.build_episode_download", new=AsyncMock(return_value=fake_download)), \
             patch("services.disk_space.shutil.disk_usage", return_value=disk_usage), \
             patch("services.disk_space.os.path.exists", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                await download_series(
                    data=data, auth=_make_auth(), session=_make_session()
                )

        self.assertEqual(ctx.exception.status_code, 507)


if __name__ == "__main__":
    unittest.main()
