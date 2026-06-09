"""
Regression test: _backfill_from_api crashes on null/non-dict entries in epg_listings.

Bug (MEDIUM): The inner processing loop in _backfill_from_api calls entry.get(...)
on every item returned by client.get_epg() without first checking that the item is
a dict.

Some providers return epg_listings arrays that contain null entries (e.g.
{"epg_listings": [null, {...}, null, {...}]}) due to encoding bugs or list-padding.

When entry is None, entry.get("start_timestamp") raises:
    AttributeError: 'NoneType' object has no attribute 'get'

This AttributeError is NOT caught by the per-channel try/except block, which only
wraps the client.get_epg() call. The exception propagates out of _backfill_from_api
entirely, causing all remaining channels to be skipped.

Expected behavior (after fix): null and non-dict entries in epg_listings are silently
skipped; valid entries in the same batch and all remaining channels are processed
normally.

Root cause: epg_ingest_manager.py, the "for entry in epg_entries" loop: no
isinstance(entry, dict) guard before calling entry.get().
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import models  # ensure all tables are registered in Base.metadata
from database import Base
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from models import EPGProgram
from services.epg_ingest_manager import EPGIngestManager, _program_insert_stmt


def _make_valid_entry(now_utc):
    start = now_utc - timedelta(days=2)
    stop = start + timedelta(hours=1)
    return {
        "start_timestamp": str(int(start.timestamp())),
        "stop_timestamp": str(int(stop.timestamp())),
    }


def _channel_target(stream_id, now_utc, archive_days=7):
    channel = {"stream_id": stream_id, "name": f"Channel {stream_id}", "tv_archive": 1}
    backfill_end = now_utc
    return (channel, backfill_end, archive_days)


class BackfillNullEntryTests(unittest.IsolatedAsyncioTestCase):
    """_backfill_from_api must skip null/non-dict entries without crashing.

    These tests call the real _backfill_from_api method against an in-memory
    SQLite database. Removing the isinstance(entry, dict) guard from the source
    will cause test_null_entry_before_valid_entry and
    test_string_entry_before_valid_entry to fail (AttributeError propagates out
    of the method), which is the intended regression protection.
    """

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.manager = EPGIngestManager()
        self.now_utc = datetime.now(timezone.utc)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _run_backfill(self, client, channel_targets):
        insert_stmt = _program_insert_stmt()
        with patch("services.epg_ingest_manager.async_session_maker", self.session_maker):
            with patch("services.epg_ingest_manager.backend_log_stream") as mock_stream:
                mock_stream.emit = AsyncMock()
                processed, inserted, _all_failed = await self.manager._backfill_from_api(
                    client=client,
                    channel_targets=channel_targets,
                    now_utc=self.now_utc,
                    processed=0,
                    inserted=0,
                    account_id=1,
                    insert_stmt=insert_stmt,
                )
                return processed, inserted

    async def _program_count(self):
        async with self.session_maker() as session:
            result = await session.execute(select(EPGProgram))
            return len(result.scalars().all())

    async def test_null_entry_before_valid_entry(self):
        """A null entry before a valid entry: null is skipped, valid entry is inserted.

        Bug: [None, valid] causes AttributeError on None.get(), which aborts
        _backfill_from_api entirely. The valid entry is never inserted and all
        subsequent channels are skipped.

        After fix: processed=1, inserted=1.
        """
        valid = _make_valid_entry(self.now_utc)
        client = _FakeClient({"101": [None, valid]})
        targets = [_channel_target("101", self.now_utc)]

        processed, inserted = await self._run_backfill(client, targets)

        self.assertEqual(
            processed,
            1,
            f"Expected 1 valid entry processed after null was skipped, got {processed}. "
            "Bug: None.get() raises AttributeError before the valid entry is reached. "
            "Fix: add 'if not isinstance(entry, dict): continue' before entry.get() "
            "in _backfill_from_api.",
        )
        self.assertEqual(inserted, 1, f"Expected 1 row inserted, got {inserted}.")
        self.assertEqual(await self._program_count(), 1)

    async def test_string_entry_before_valid_entry(self):
        """A string entry (e.g. 'programme_id_123') before a valid entry: skipped, valid inserted.

        Some providers return ['programme_id_123', {...}] where the leading string
        is a channel identifier, not a program dict. str.get() raises AttributeError.

        After fix: processed=1, inserted=1.
        """
        valid = _make_valid_entry(self.now_utc)
        client = _FakeClient({"102": ["programme_id_123", valid]})
        targets = [_channel_target("102", self.now_utc)]

        processed, inserted = await self._run_backfill(client, targets)

        self.assertEqual(
            processed,
            1,
            f"Expected 1 valid entry processed after string was skipped, got {processed}. "
            "Bug: str.get() raises AttributeError. "
            "Fix: isinstance(entry, dict) guard in _backfill_from_api.",
        )
        self.assertEqual(inserted, 1, f"Expected 1 row inserted, got {inserted}.")

    async def test_second_channel_processed_after_null_in_first(self):
        """Channel B's entries must be inserted even when channel A returns only null entries.

        Bug: AttributeError in channel A's loop aborts _backfill_from_api entirely.
        Channel B is never reached, leaving its EPG permanently empty.

        After fix: channel A yields 0 rows; channel B yields 1 row inserted.
        """
        valid = _make_valid_entry(self.now_utc)
        client = _FakeClient({
            "201": [None],
            "202": [valid],
        })
        targets = [
            _channel_target("201", self.now_utc),
            _channel_target("202", self.now_utc),
        ]

        processed, inserted = await self._run_backfill(client, targets)

        self.assertEqual(
            processed,
            1,
            f"Expected channel B's 1 valid entry to be processed, got {processed}. "
            "Bug: null in channel A aborts the entire backfill, channel B is never reached.",
        )
        self.assertEqual(await self._program_count(), 1,
                         "Channel B's program must be in the DB.")

    async def test_all_valid_entries_processed(self):
        """Sanity: a normal all-dict epg_listings must still be fully processed."""
        now = self.now_utc
        entry_a = {
            "start_timestamp": str(int((now - timedelta(days=2)).timestamp())),
            "stop_timestamp": str(int((now - timedelta(days=2) + timedelta(hours=1)).timestamp())),
        }
        entry_b = {
            "start_timestamp": str(int((now - timedelta(days=1)).timestamp())),
            "stop_timestamp": str(int((now - timedelta(days=1) + timedelta(hours=1)).timestamp())),
        }
        client = _FakeClient({"103": [entry_a, entry_b]})
        targets = [_channel_target("103", self.now_utc)]

        processed, inserted = await self._run_backfill(client, targets)

        self.assertEqual(processed, 2, f"Expected 2 entries processed, got {processed}.")
        self.assertEqual(inserted, 2, f"Expected 2 rows inserted, got {inserted}.")


class _FakeClient:
    def __init__(self, entries_by_stream_id):
        self._entries = entries_by_stream_id

    async def get_epg(self, stream_id: str) -> list:
        return self._entries.get(stream_id, [])


if __name__ == "__main__":
    unittest.main()
