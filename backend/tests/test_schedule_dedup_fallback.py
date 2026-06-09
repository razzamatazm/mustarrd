"""
Regression test for: schedule dedup skipped when program has no epg_id and no id.

Root cause: POST /schedules had dedup checks only for the epg_id branch and the
program_id branch. When both were absent (providers that strip EPG metadata), no
dedup ran and a second POST with the same (account_id, channel_id, start_timestamp,
stop_timestamp) created a second ScheduledRecording row. Both fired at dispatch,
producing two Download rows targeting the same output path and corrupting the file.

Fix: add an else branch that deduplicates on
(account_id, channel_id, start_timestamp, stop_timestamp, active status).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from api.schedules import ScheduleCreate, create_schedule


_PROGRAM_NO_IDS = {
    "title": "Newsnight",
    "start_timestamp": 2000000000,
    "stop_timestamp": 2000003600,
}


def _make_auth(is_admin=True, user_id=1):
    auth = MagicMock()
    auth.is_admin = is_admin
    auth.user_id = user_id
    auth.provider = "admin_local"
    return auth


def _make_account():
    from models import XtreamAccount
    account = MagicMock(spec=XtreamAccount)
    account.id = 1
    return account


def _make_session(responses):
    """Mock session whose execute() returns scalar_one_or_none() from responses in order."""
    idx = [0]

    async def execute(stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = responses[idx[0]]
        idx[0] += 1
        return result

    session = AsyncMock()
    session.execute = execute
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


class ScheduleDedupFallbackTests(unittest.IsolatedAsyncioTestCase):
    """
    Dedup must fire on (account_id, channel_id, start_timestamp, stop_timestamp)
    when the program payload carries neither epg_id nor id.
    """

    async def test_first_schedule_without_ids_succeeds(self):
        """First POST with no epg_id/program_id must create the schedule (HTTP 201)."""
        data = ScheduleCreate(
            account_id=1,
            channel_id="CH1",
            channel_name="BBC One",
            program=_PROGRAM_NO_IDS,
        )
        auth = _make_auth()
        # execute calls: account lookup (found), dedup check (no duplicate)
        session = _make_session([_make_account(), None])

        result = await create_schedule(data=data, auth=auth, session=session)

        self.assertIsInstance(result, dict, "First schedule must return a dict response.")

    async def test_duplicate_schedule_without_ids_returns_409(self):
        """Second POST with same timestamps and no epg_id/program_id must return 409."""
        data = ScheduleCreate(
            account_id=1,
            channel_id="CH1",
            channel_name="BBC One",
            program=_PROGRAM_NO_IDS,
        )
        auth = _make_auth()
        existing = MagicMock()
        # execute calls: account lookup (found), dedup check (duplicate found)
        session = _make_session([_make_account(), existing])

        with self.assertRaises(HTTPException) as ctx:
            await create_schedule(data=data, auth=auth, session=session)

        self.assertEqual(
            ctx.exception.status_code,
            409,
            "Duplicate schedule without epg_id/program_id must raise 409.",
        )

    async def test_dedup_not_triggered_for_different_channel(self):
        """Different channel_id must not be treated as a duplicate."""
        data = ScheduleCreate(
            account_id=1,
            channel_id="CH2",
            channel_name="ITV",
            program=_PROGRAM_NO_IDS,
        )
        auth = _make_auth()
        # dedup check returns None: different channel, no match
        session = _make_session([_make_account(), None])

        result = await create_schedule(data=data, auth=auth, session=session)

        self.assertIsInstance(result, dict, "Different channel must succeed.")


if __name__ == "__main__":
    unittest.main()
