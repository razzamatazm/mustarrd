"""
Regression tests: schedule dedupe must consider all users' schedules.

Before fix: POST /schedules only checked the requesting user's own schedules
for non-admins (conditions included requested_by_user_id == auth.user_id), so:

  1. A non-admin could duplicate a program an admin had already scheduled.
  2. Two non-admin users could schedule the same program; both rows dispatched
     to the same output file and the second silently FAILED at dispatch time
     with a raw "already active for this output file" error.

After fix: the dedupe query covers every user's active schedule for the same
account+channel+program+timeslot. The second request gets an immediate,
clear 409 ("already scheduled by another user") instead of a silent
dispatch-time failure.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import ScheduledRecording, User, XtreamAccount
from api.schedules import ScheduleCreate, create_schedule


def _future_program(epg_id="ep-1"):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    stop = start + timedelta(hours=1)
    return {
        "title": "Newsnight",
        "epg_id": epg_id,
        "id": "prog-1",
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(stop.timestamp()),
    }


def _auth(user_id, is_admin=False):
    auth = MagicMock()
    auth.user_id = user_id
    auth.is_admin = is_admin
    auth.provider = "admin_local" if is_admin else "local"
    return auth


class ScheduleCrossUserDedupTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                name="acc", server_url="http://provider.test",
                username="u", password="p",
            ))
            session.add(User(id=1, role="admin", username="admin"))
            session.add(User(id=2, role="download_only", username="alice"))
            session.add(User(id=3, role="download_only", username="bob"))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _request(self, program):
        return ScheduleCreate(
            account_id=1,
            channel_id="CH1",
            channel_name="BBC One",
            program=program,
        )

    async def _create(self, auth, program):
        async with self.session_factory() as session:
            return await create_schedule(
                data=self._request(program), auth=auth, session=session
            )

    async def _count_schedules(self):
        async with self.session_factory() as session:
            result = await session.execute(select(ScheduledRecording))
            return len(result.scalars().all())

    async def test_non_admin_cannot_duplicate_admin_schedule(self):
        """REGRESSION (item 7): a non-admin scheduling a program the admin has
        already scheduled must get 409, not a second schedule row."""
        program = _future_program()
        await self._create(_auth(1, is_admin=True), program)

        with self.assertRaises(HTTPException) as ctx:
            await self._create(_auth(2), program)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            await self._count_schedules(), 1,
            "The duplicate request must not create a second schedule row.",
        )

    async def test_second_non_admin_gets_clear_409(self):
        """REGRESSION (item 8): when two non-admin users schedule the same
        program, the second gets an immediate, readable 409 instead of a
        schedule that silently fails at dispatch time."""
        program = _future_program()
        await self._create(_auth(2), program)

        with self.assertRaises(HTTPException) as ctx:
            await self._create(_auth(3), program)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(
            "another user", ctx.exception.detail,
            "The message must explain the program is held by another user's schedule.",
        )
        self.assertEqual(await self._count_schedules(), 1)

    async def test_same_user_duplicate_still_409(self):
        """Guard: the same user re-scheduling their own program still gets 409
        with the original message."""
        program = _future_program()
        await self._create(_auth(2), program)

        with self.assertRaises(HTTPException) as ctx:
            await self._create(_auth(2), program)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "This program is already scheduled")

    async def test_admin_blocked_by_non_admin_schedule(self):
        """Guard: admins are still deduped against everyone's schedules."""
        program = _future_program()
        await self._create(_auth(2), program)

        with self.assertRaises(HTTPException) as ctx:
            await self._create(_auth(1, is_admin=True), program)

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_different_program_not_deduped(self):
        """Sanity: a different timeslot/epg_id on the same channel is allowed."""
        await self._create(_auth(2), _future_program(epg_id="ep-1"))

        other = _future_program(epg_id="ep-2")
        other["start_timestamp"] += 7200
        other["stop_timestamp"] += 7200
        result = await self._create(_auth(3), other)

        self.assertIsInstance(result, dict)
        self.assertEqual(await self._count_schedules(), 2)


if __name__ == "__main__":
    unittest.main()
