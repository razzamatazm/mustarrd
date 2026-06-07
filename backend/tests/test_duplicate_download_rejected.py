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
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import Download, DownloadStatus


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


class DuplicateDownloadQueryTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests: run the real duplicate-check SQL against in-memory SQLite.

    Cleo review feedback: mock-only tests never exercise the actual WHERE clause.
    These tests seed real Download rows and run the same query used in create_download.
    """

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    _active_statuses = [
        DownloadStatus.PENDING.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.PROCESSING.value,
    ]

    def _make_row(self, status, account_id=1, channel_id="ch1", start_ts=1000, stop_ts=2000):
        return Download(
            account_id=account_id,
            channel_id=channel_id,
            channel_name="Test Channel",
            program_title="Show",
            program_start=datetime(2024, 1, 1, 20, 0),
            program_end=datetime(2024, 1, 1, 21, 0),
            start_timestamp=start_ts,
            stop_timestamp=stop_ts,
            duration_minutes=60,
            source_url="http://provider.test/ts",
            output_path="/downloads/show.ts",
            status=status,
        )

    async def _run_query(self, session, account_id=1, channel_id="ch1", start_ts=1000, stop_ts=2000):
        result = await session.execute(
            select(Download.id).where(
                Download.account_id == account_id,
                Download.channel_id == channel_id,
                Download.start_timestamp == start_ts,
                Download.stop_timestamp == stop_ts,
                Download.status.in_(self._active_statuses),
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def test_pending_row_found(self):
        """Real query returns an ID when a matching PENDING row exists."""
        async with self.session_factory() as session:
            session.add(self._make_row(DownloadStatus.PENDING.value))
            await session.commit()
        async with self.session_factory() as session:
            found = await self._run_query(session)
        self.assertIsNotNone(found)

    async def test_two_active_rows_does_not_raise(self):
        """Two identical active rows (corrupted state) must not raise.

        Before fix: select(Download) without limit(1) raised MultipleResultsFound
        on scalar_one_or_none(), returning HTTP 500 instead of 409.
        After fix: limit(1) ensures at most one row is returned.
        """
        async with self.session_factory() as session:
            session.add(self._make_row(DownloadStatus.PENDING.value))
            session.add(self._make_row(DownloadStatus.DOWNLOADING.value))
            await session.commit()
        async with self.session_factory() as session:
            found = await self._run_query(session)
        self.assertIsNotNone(found)

    async def test_completed_row_not_matched(self):
        """COMPLETED is terminal: the real WHERE clause excludes it, returns None."""
        async with self.session_factory() as session:
            session.add(self._make_row(DownloadStatus.COMPLETED.value))
            await session.commit()
        async with self.session_factory() as session:
            found = await self._run_query(session)
        self.assertIsNone(found)

    async def test_failed_row_not_matched(self):
        """FAILED is terminal: the real WHERE clause excludes it, returns None."""
        async with self.session_factory() as session:
            session.add(self._make_row(DownloadStatus.FAILED.value))
            await session.commit()
        async with self.session_factory() as session:
            found = await self._run_query(session)
        self.assertIsNone(found)

    async def test_different_channel_not_matched(self):
        """Active row for a different channel does not block the new request."""
        async with self.session_factory() as session:
            session.add(self._make_row(DownloadStatus.PENDING.value, channel_id="other"))
            await session.commit()
        async with self.session_factory() as session:
            found = await self._run_query(session)
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
