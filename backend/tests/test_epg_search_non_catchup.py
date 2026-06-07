"""
EPG search must not return past programs with has_archive=False.
Those are stored by the backfill path when a provider marks individual
programs as unavailable for catchup, but a user clicking them in the
browse page gets no feedback and no download. Filtering them out at
query time keeps search results to programs the user can actually act on.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import String, Integer, DateTime, Text, Boolean, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from api.epg import _build_like_pattern


class _Base(DeclarativeBase):
    pass


class _EPGProgram(_Base):
    __tablename__ = "epg_programs"
    __table_args__ = (
        Index("ix_epg_programs_account_channel_start", "account_id", "channel_id", "start_time"),
        Index("ux_epg_programs_account_epg_id", "account_id", "epg_id", unique=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[str] = mapped_column(String(100))
    channel_name: Mapped[str] = mapped_column(String(255))
    xmltv_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    epg_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    start_timestamp: Mapped[int] = mapped_column(Integer)
    stop_timestamp: Mapped[int] = mapped_column(Integer)
    provider_start: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_stop: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    has_archive: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def _prog(epg_id, title, end_time, has_archive, account_id=1):
    start_time = end_time - timedelta(hours=1)
    return _EPGProgram(
        account_id=account_id,
        channel_id="1",
        channel_name="Test Channel",
        epg_id=epg_id,
        title=title,
        description=None,
        category=None,
        start_time=start_time,
        end_time=end_time,
        start_timestamp=int(start_time.timestamp()),
        stop_timestamp=int(end_time.timestamp()),
        duration_minutes=60,
        has_archive=has_archive,
    )


async def _run_search(session, account_id, q, now_utc):
    pattern = _build_like_pattern(q.lower())
    result = await session.execute(
        select(_EPGProgram)
        .where(
            _EPGProgram.account_id == account_id,
            or_(
                func.lower(_EPGProgram.title).like(pattern, escape="\\"),
                func.lower(_EPGProgram.description).like(pattern, escape="\\"),
            ),
            or_(
                _EPGProgram.end_time > now_utc,
                _EPGProgram.has_archive == True,
            ),
        )
        .order_by(_EPGProgram.start_time.desc())
    )
    return result.scalars().all()


class EPGSearchNonCatchupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_past_no_archive_excluded(self):
        """A past program with has_archive=False must not appear in search results."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_prog("p1", "Breaking Bad", now - timedelta(days=1), has_archive=False))
            await session.commit()
            rows = await _run_search(session, 1, "Breaking Bad", now)
        self.assertEqual(rows, [])

    async def test_past_with_archive_included(self):
        """A past program with has_archive=True must appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_prog("p2", "Breaking Bad", now - timedelta(days=1), has_archive=True))
            await session.commit()
            rows = await _run_search(session, 1, "Breaking Bad", now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].epg_id, "p2")

    async def test_future_no_archive_included(self):
        """A future program with has_archive=False must appear (it is schedulable)."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_prog("p3", "Breaking Bad", now + timedelta(days=1), has_archive=False))
            await session.commit()
            rows = await _run_search(session, 1, "Breaking Bad", now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].epg_id, "p3")

    async def test_future_with_archive_included(self):
        """A future program with has_archive=True must appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_prog("p4", "Breaking Bad", now + timedelta(days=1), has_archive=True))
            await session.commit()
            rows = await _run_search(session, 1, "Breaking Bad", now)
        self.assertEqual(len(rows), 1)

    async def test_mixed_only_actionable_returned(self):
        """With four programs, only the two actionable ones are returned."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add_all([
                _prog("past-no",  "The Wire", now - timedelta(days=1), has_archive=False),
                _prog("past-yes", "The Wire", now - timedelta(days=2), has_archive=True),
                _prog("fut-no",   "The Wire", now + timedelta(days=1), has_archive=False),
                _prog("fut-yes",  "The Wire", now + timedelta(days=2), has_archive=True),
            ])
            await session.commit()
            rows = await _run_search(session, 1, "The Wire", now)
        epg_ids = {r.epg_id for r in rows}
        self.assertNotIn("past-no", epg_ids)
        self.assertIn("past-yes", epg_ids)
        self.assertIn("fut-no", epg_ids)
        self.assertIn("fut-yes", epg_ids)
        self.assertEqual(len(rows), 3)

    async def test_different_account_not_returned(self):
        """Programs from a different account must not appear."""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(_prog("p5", "Sopranos", now - timedelta(days=1), has_archive=True, account_id=2))
            await session.commit()
            rows = await _run_search(session, 1, "Sopranos", now)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
