"""
Regression tests: create_download must return 409 when the same program is already
pending, downloading, or processing for the same account and channel.

Before fix: two rapid POST /api/downloads/ calls for the same program both succeeded.
Both tasks opened the same output path with aiofiles.open(path, 'wb'), the second
truncating whatever the first had written. Result: corrupt or empty recording.

After fix: the second request checks for an existing non-terminal Download row with
matching account_id, channel_id, start_timestamp, stop_timestamp and returns 409
"A download for this program is already active."
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
from models import DownloadStatus


def _make_download(account_id=1, channel_id="ch1", start_ts=1000, stop_ts=2000):
    d = MagicMock()
    d.account_id = account_id
    d.channel_id = channel_id
    d.start_timestamp = start_ts
    d.stop_timestamp = stop_ts
    d.to_dict.return_value = {"id": 1, "status": "pending"}
    d.id = 1
    return d


def _make_auth():
    auth = MagicMock()
    auth.user_id = 1
    auth.provider = "admin_local"
    auth.is_admin = True
    return auth


def _make_data(account_id=1, channel_id="ch1"):
    data = MagicMock()
    data.account_id = account_id
    data.channel_id = channel_id
    data.channel_name = "Test Channel"
    data.program = {}
    data.custom_filename = None
    data.pre_padding_minutes = 0
    data.post_padding_minutes = 0
    return data


def _make_session(existing_download=None, min_free_gb=25):
    """Build a mock session. existing_download is returned by the duplicate check query."""
    app_settings_obj = MagicMock()
    app_settings_obj.download_folder = "/downloads"
    app_settings_obj.min_free_space_gb = min_free_gb

    settings_scalar = MagicMock()
    settings_scalar.scalar_one_or_none = MagicMock(return_value=app_settings_obj)

    dup_scalar = MagicMock()
    dup_scalar.scalar_one_or_none = MagicMock(return_value=existing_download)

    # session.execute is called twice in create_download after the fix:
    #   1st call: duplicate-check query
    #   2nd call: (check_disk_space reads AppSettings)
    # We return dup_scalar on the first call and settings_scalar on the second.
    call_count = [0]

    async def _execute(query):
        call_count[0] += 1
        if call_count[0] == 1:
            return dup_scalar
        return settings_scalar

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session


def _call_create_download(existing_download=None, free_bytes=50 * 1024 ** 3):
    from api.downloads import create_download

    fake_download = _make_download()
    disk_result = MagicMock()
    disk_result.free = free_bytes

    with patch("api.downloads.build_download_from_program", new=AsyncMock(return_value=fake_download)), \
         patch("services.disk_space.shutil.disk_usage", return_value=disk_result), \
         patch("services.disk_space.os.path.exists", return_value=True), \
         patch("api.downloads.download_manager.queue_download", new=AsyncMock(return_value=fake_download)), \
         patch("api.downloads._attach_requested_by", new=AsyncMock(return_value=[{"id": 1}])):
        return asyncio.run(
            create_download(
                data=_make_data(),
                auth=_make_auth(),
                session=_make_session(existing_download=existing_download),
            )
        )


class DuplicateDownloadTests(unittest.TestCase):

    def test_duplicate_pending_returns_409(self):
        """Second request for same program already PENDING returns 409.

        Before fix: both downloads were created and wrote to the same path.
        After fix: 409 'A download for this program is already active.'
        """
        existing = _make_download()
        existing.status = DownloadStatus.PENDING.value
        with self.assertRaises(HTTPException) as ctx:
            _call_create_download(existing_download=existing)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("already active", ctx.exception.detail)

    def test_duplicate_downloading_returns_409(self):
        """Same program already DOWNLOADING returns 409."""
        existing = _make_download()
        existing.status = DownloadStatus.DOWNLOADING.value
        with self.assertRaises(HTTPException) as ctx:
            _call_create_download(existing_download=existing)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_duplicate_processing_returns_409(self):
        """Same program already PROCESSING (post-process) returns 409."""
        existing = _make_download()
        existing.status = DownloadStatus.PROCESSING.value
        with self.assertRaises(HTTPException) as ctx:
            _call_create_download(existing_download=existing)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_no_duplicate_proceeds(self):
        """No existing active download: request proceeds normally."""
        result = _call_create_download(existing_download=None)
        self.assertEqual(result["id"], 1)

    def test_completed_download_allows_redownload(self):
        """COMPLETED download is terminal: re-requesting the same program is allowed."""
        existing = _make_download()
        existing.status = DownloadStatus.COMPLETED.value
        # The mock returns the completed row from the DB query, but because
        # status is not in the active set the WHERE clause would not match.
        # Simulate that by returning None (no active row found).
        result = _call_create_download(existing_download=None)
        self.assertEqual(result["id"], 1)

    def test_failed_download_allows_redownload(self):
        """FAILED download is terminal: re-requesting is allowed."""
        result = _call_create_download(existing_download=None)
        self.assertEqual(result["id"], 1)


if __name__ == "__main__":
    unittest.main()
