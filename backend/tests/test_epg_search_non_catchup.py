"""
EPG search must not return past programs with has_archive=False.
Those are stored by the backfill path when a provider marks individual
programs as unavailable for catchup, but a user clicking them in the
browse page gets no feedback and no download. Filtering them out at
query time keeps search results to programs the user can actually act on.

guide_offset_hours: the filter threshold is adjusted by the account's
guide offset so that the UI's clickability judgment and the filter agree.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import Base
from models import EPGProgram, XtreamAccount, AppSettings
from api.epg import search_epg


def _account(account_id=1, guide_offset_hours=0):
    return XtreamAccount(
        id=account_id,
        name="Test Provider",
        server_url="http://provider.test",
        username="user",
        password="pass",
        guide_offset_hours=guide_offset_hours,
    )


def _prog(epg_id, title, end_time, has_archive, account_id=1):
    start_time = end_time - timedelta(hours=1)
    return EPGProgram(
        account_id=account_id,
        channel_id="1",
        channel_name="Test Channel",
        epg_id=epg_id,
        title=title,
        description=None,
        category=None,
        start_time=start_time,
        end_time=end_time,
        start_timestamp=int(start_time.replace(tzinfo=None).timestamp() if start_time.tzinfo else start_time.timestamp()),
        stop_timestamp=int(end_time.replace(tzinfo=None).timestamp() if end_time.tzinfo else end_time.timestamp()),
        duration_minutes=60,
        has_archive=has_archive,
    )


class EPGSearchNonCatchupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _search(self, session, account_id, q, limit=100, offset=0):
        """Call the real search_epg handler, bypassing FastAPI dependency injection."""
        return await search_epg(
            account_id=account_id,
            q=q,
            limit=limit,
            offset=offset,
            _admin=None,
            session=session,
        )

    async def test_past_no_archive_excluded(self):
        """A past program with has_archive=False must not appear in search results."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(_prog("p1", "Breaking Bad", now - timedelta(days=1), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "Breaking Bad")
        self.assertEqual(rows, [])

    async def test_past_with_archive_included(self):
        """A past program with has_archive=True must appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(_prog("p2", "Breaking Bad", now - timedelta(days=1), has_archive=True))
            await session.commit()
            rows = await self._search(session, 1, "Breaking Bad")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epg_id"], "p2")

    async def test_future_no_archive_included(self):
        """A future program with has_archive=False must appear (it is schedulable)."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(_prog("p3", "Breaking Bad", now + timedelta(days=1), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "Breaking Bad")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epg_id"], "p3")

    async def test_future_with_archive_included(self):
        """A future program with has_archive=True must appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(_prog("p4", "Breaking Bad", now + timedelta(days=1), has_archive=True))
            await session.commit()
            rows = await self._search(session, 1, "Breaking Bad")
        self.assertEqual(len(rows), 1)

    async def test_mixed_only_actionable_returned(self):
        """With four programs, only the three actionable ones are returned."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add_all([
                _prog("past-no",  "The Wire", now - timedelta(days=1), has_archive=False),
                _prog("past-yes", "The Wire", now - timedelta(days=2), has_archive=True),
                _prog("fut-no",   "The Wire", now + timedelta(days=1), has_archive=False),
                _prog("fut-yes",  "The Wire", now + timedelta(days=2), has_archive=True),
            ])
            await session.commit()
            rows = await self._search(session, 1, "The Wire")
        epg_ids = {r["epg_id"] for r in rows}
        self.assertNotIn("past-no", epg_ids)
        self.assertIn("past-yes", epg_ids)
        self.assertIn("fut-no", epg_ids)
        self.assertIn("fut-yes", epg_ids)
        self.assertEqual(len(rows), 3)

    async def test_different_account_not_returned(self):
        """Programs from a different account must not appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account(account_id=1))
            session.add(_account(account_id=2))
            session.add(_prog("p5", "Sopranos", now - timedelta(days=1), has_archive=True, account_id=2))
            await session.commit()
            rows = await self._search(session, 1, "Sopranos")
        self.assertEqual(rows, [])

    async def test_negative_offset_hides_display_past_program(self):
        """With guide_offset_hours=-2, a program whose display end is past must be hidden.

        A program ending 1 hour ago (raw UTC) displays as ending 3 hours ago with
        a -2h offset. It should be filtered out unless it has archive.
        """
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account(guide_offset_hours=-2))
            # end_time = now - 1h; display end = (now-1h) + (-2h) = now - 3h (past in display)
            session.add(_prog("offset-past", "Fargo", now - timedelta(hours=1), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "Fargo")
        self.assertEqual(rows, [])

    async def test_positive_offset_keeps_display_future_program(self):
        """With guide_offset_hours=+2, a program whose display end is still future must be kept.

        A program ending 1 hour ago (raw UTC) displays as ending 1 hour from now with
        a +2h offset. It should appear even without archive.
        """
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account(guide_offset_hours=2))
            # end_time = now - 1h; display end = (now-1h) + 2h = now + 1h (future in display)
            session.add(_prog("offset-future", "Fargo", now - timedelta(hours=1), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "Fargo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epg_id"], "offset-future")

    async def test_global_offset_minutes_keeps_near_threshold_program(self):
        """A positive epg_offset_minutes shifts the search threshold back, including
        a non-archive program that raw-UTC filtering would exclude.

        Program ends 30 min ago (raw UTC). No account offset. Without global offset it
        falls past the threshold (end_time < now) and is hidden. With
        epg_offset_minutes=60, threshold = now - 60 min, so end_time = now - 30 min
        passes and the row is returned.
        """
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(AppSettings(epg_offset_minutes=60))
            # end_time 30 min ago: excluded without global offset, included with +60 min offset
            session.add(_prog("global-offset-keep", "Breaking Bad", now - timedelta(minutes=30), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "Breaking Bad")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epg_id"], "global-offset-keep")

    async def test_global_offset_minutes_excludes_too_old_program(self):
        """A positive epg_offset_minutes does not resurrect programs far in the past.

        Program ends 2 hours ago with epg_offset_minutes=60: threshold = now - 60 min,
        end_time = now - 2h still fails. Row excluded without archive.
        """
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_account())
            session.add(AppSettings(epg_offset_minutes=60))
            session.add(_prog("global-offset-exclude", "The Wire", now - timedelta(hours=2), has_archive=False))
            await session.commit()
            rows = await self._search(session, 1, "The Wire")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
