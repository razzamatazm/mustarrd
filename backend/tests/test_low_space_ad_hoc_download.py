"""
Regression tests: create_download must reject ad-hoc downloads when disk is below
the min_free_space_gb threshold.

Before fix: min_free_space_gb was only checked in scheduled_manager. A user clicking
Download on Browse EPG bypassed the guard. The download started, ran until it hit an
OSError (ENOSPC) or filled the disk, and showed a cryptic error with no disk-space
context.

After fix: create_download checks free space before queuing and returns 507
Insufficient Storage with a user-readable message.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException


def _make_session(min_free_gb=25, download_folder="/downloads"):
    app_settings = MagicMock()
    app_settings.download_folder = download_folder
    app_settings.min_free_space_gb = min_free_gb

    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=app_settings)

    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar_result)
    return session


def _make_auth():
    auth = MagicMock()
    auth.user_id = 1
    auth.provider = "admin_local"
    auth.is_admin = True
    return auth


def _make_data():
    data = MagicMock()
    data.account_id = 1
    data.channel_id = "ch1"
    data.channel_name = "Test Channel"
    data.program = {}
    data.custom_filename = None
    data.pre_padding_minutes = 0
    data.post_padding_minutes = 0
    return data


def _make_fake_download():
    fake = MagicMock()
    fake.program_title = "Test Show"
    fake.channel_name = "Test Channel"
    fake.to_dict.return_value = {"id": 1, "status": "pending"}
    fake.id = 1
    return fake


def _call_create_download(free_bytes, min_free_gb=25, folder_exists=True):
    from api.downloads import create_download

    disk_result = MagicMock()
    disk_result.free = free_bytes

    with patch("api.downloads.build_download_from_program", new=AsyncMock(return_value=_make_fake_download())), \
         patch("api.downloads.shutil.disk_usage", return_value=disk_result), \
         patch("api.downloads.os.path.exists", return_value=folder_exists), \
         patch("api.downloads.download_manager.queue_download", new=AsyncMock(return_value=_make_fake_download())), \
         patch("api.downloads._attach_requested_by", new=AsyncMock(return_value=[{"id": 1}])):
        return asyncio.run(
            create_download(data=_make_data(), auth=_make_auth(), session=_make_session(min_free_gb=min_free_gb))
        )


class LowSpaceAdHocDownloadTests(unittest.TestCase):

    def test_low_space_raises_507(self):
        """507 raised when free space is below min_free_space_gb.

        Before fix: download queued anyway, failing later with a cryptic OSError.
        After fix: 507 Insufficient Storage with a human-readable message.
        """
        with self.assertRaises(HTTPException) as ctx:
            _call_create_download(free_bytes=10 * 1024 ** 3, min_free_gb=25)
        self.assertEqual(ctx.exception.status_code, 507)
        self.assertIn("disk space", ctx.exception.detail.lower())

    def test_sufficient_space_proceeds(self):
        """Download queues normally when space is above threshold."""
        result = _call_create_download(free_bytes=50 * 1024 ** 3, min_free_gb=25)
        self.assertEqual(result["id"], 1)

    def test_exact_threshold_is_allowed(self):
        """free_gb == min_free_space_gb is not rejected (consistent with scheduled_manager)."""
        result = _call_create_download(free_bytes=25 * 1024 ** 3, min_free_gb=25)
        self.assertEqual(result["id"], 1)

    def test_missing_folder_skips_check(self):
        """If download folder does not exist yet, skip the check and proceed."""
        result = _call_create_download(
            free_bytes=1 * 1024 ** 3, min_free_gb=25, folder_exists=False
        )
        self.assertEqual(result["id"], 1)

    def test_zero_min_free_gb_disables_check(self):
        """min_free_space_gb=0 disables the guard so the download proceeds."""
        result = _call_create_download(free_bytes=1 * 1024 ** 3, min_free_gb=0)
        self.assertEqual(result["id"], 1)


if __name__ == "__main__":
    unittest.main()
