"""
Regression test: EPG titles and descriptions frozen forever when provider corrects metadata.

Bug (MEDIUM): EPGProgram rows are inserted with INSERT OR IGNORE against a unique
index on (account_id, epg_id), where
    epg_id = f"{stream_id}:{start_timestamp}:{stop_timestamp}"

When a provider corrects a program title or description (same channel, same
air time, different metadata), the OR IGNORE clause silently discards the
update. The stale title persists until the row's end_time falls below the
archive cutoff and is cleaned up in the next refresh cycle, which can be
days away for channels with long archive windows.

User impact: a user sees the wrong program title in the guide (e.g. a news
special mislabeled by the provider, a live event with a placeholder title,
or a typo corrected by the provider after initial ingest). Re-downloading or
re-scheduling based on the stale guide entry records the wrong content with
the wrong filename.

Root cause: epg_ingest_manager._program_insert_stmt()
    returns insert(EPGProgram).prefix_with("OR IGNORE")

There is no UPDATE or ON CONFLICT DO UPDATE path. Once a row is in the DB,
its title and description are frozen until the row expires.

Fix required: change _program_insert_stmt() to use ON CONFLICT DO UPDATE SET
title=..., description=..., category=... (leaving start_time, end_time,
channel_id unchanged since those form the identity key).
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import models  # import all models so Base.metadata includes every table
from database import Base
from models import EPGProgram
from services.epg_ingest_manager import _program_insert_stmt


def _program_row(epg_id, title, description="", account_id=1):
    """Return a minimal EPGProgram dict for bulk insert, mirroring the
    batch dicts built in epg_ingest_manager.refresh_epg_for_account()."""
    start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    return {
        "account_id": account_id,
        "epg_id": epg_id,
        "channel_id": "101",
        "channel_name": "Test Channel",
        "title": title,
        "description": description,
        "category": None,
        "start_time": start,
        "end_time": end,
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(end.timestamp()),
        "duration_minutes": 60,
        "has_archive": True,
        "provider_start": None,
        "provider_stop": None,
        "xmltv_id": None,
    }


class StaleTitleOnProviderUpdateTests(unittest.IsolatedAsyncioTestCase):
    """INSERT OR IGNORE silently discards provider-corrected titles and descriptions."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _insert(self, rows):
        async with self.session_maker() as session:
            async with session.begin():
                await session.execute(_program_insert_stmt(), rows)

    async def _fetch(self, account_id, epg_id):
        async with self.session_maker() as session:
            result = await session.execute(
                select(EPGProgram).where(
                    EPGProgram.account_id == account_id,
                    EPGProgram.epg_id == epg_id,
                )
            )
            return result.scalar_one_or_none()

    async def test_corrected_title_visible_after_provider_update(self):
        """After a provider corrects a program title, the next EPG refresh must
        store the updated title in the guide.

        Bug: INSERT OR IGNORE keeps the first-inserted title forever. A program
        initially stored as 'Placeholder Title' cannot have its title corrected
        to 'Live: World Cup Final' even after the provider fixes its guide data.
        The user sees 'Placeholder Title' until the row expires (could be days).

        Expected behavior: second ingest with the corrected title overwrites the
        stale value; the guide reflects what the provider currently says.

        This test FAILS while the bug is present and passes after the fix.
        """
        epg_id = "101:1717228800:1717232400"
        await self._insert([_program_row(epg_id, "Placeholder Title")])
        await self._insert([_program_row(epg_id, "Live: World Cup Final")])

        prog = await self._fetch(1, epg_id)
        self.assertIsNotNone(prog, "Row must exist after first insert.")
        self.assertEqual(
            prog.title,
            "Live: World Cup Final",
            f"Expected updated title 'Live: World Cup Final', got '{prog.title}'. "
            "INSERT OR IGNORE silently discards provider-corrected titles. "
            "Once a row is inserted, its title is frozen until the row expires. "
            "Fix: change _program_insert_stmt() in epg_ingest_manager.py to ON CONFLICT DO UPDATE "
            "SET title=excluded.title, description=excluded.description.",
        )

    async def test_corrected_description_visible_after_provider_update(self):
        """After a provider corrects a program description, it must be stored.

        Bug: same INSERT OR IGNORE path silently discards description updates.
        """
        epg_id = "101:1717228800:1717232400"
        await self._insert([_program_row(epg_id, "Movie Night", "TBA")])
        await self._insert([_program_row(epg_id, "Movie Night", "A classic thriller from 1978.")])

        prog = await self._fetch(1, epg_id)
        self.assertEqual(
            prog.description,
            "A classic thriller from 1978.",
            f"Expected updated description, got '{prog.description}'. "
            "INSERT OR IGNORE silently discards provider description updates.",
        )

    async def test_first_insert_still_creates_row(self):
        """Sanity: a new epg_id must still be inserted (regression guard)."""
        epg_id = "101:1717315200:1717318800"
        await self._insert([_program_row(epg_id, "News at Ten")])
        prog = await self._fetch(1, epg_id)
        self.assertIsNotNone(prog, "New row must exist after first insert.")
        self.assertEqual(prog.title, "News at Ten")

    async def test_different_account_can_have_same_epg_id(self):
        """Two accounts can have rows with the same epg_id (regression guard).
        The unique constraint is (account_id, epg_id), not just epg_id.
        """
        epg_id = "101:1717228800:1717232400"
        await self._insert([_program_row(epg_id, "Account 1 Title", account_id=1)])
        await self._insert([_program_row(epg_id, "Account 2 Title", account_id=2)])
        prog1 = await self._fetch(1, epg_id)
        prog2 = await self._fetch(2, epg_id)
        self.assertIsNotNone(prog1)
        self.assertIsNotNone(prog2)
        self.assertEqual(prog1.title, "Account 1 Title")
        self.assertEqual(prog2.title, "Account 2 Title")


if __name__ == "__main__":
    unittest.main()
