"""
Regression test: delete_account must bulk-delete all EPGProgram rows for the
account before removing the account row.

Before fix: session.delete(account) left epg_programs rows with the deleted
account_id.  Rows accumulated indefinitely, and SQLite integer PK reuse meant
a new account with the same id immediately inherited the old account's EPG data.

After fix: a DELETE FROM epg_programs WHERE account_id = ? statement runs inside
the same transaction before session.delete(account), so no orphaned rows remain.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.accounts import delete_account
from models import AppSettings, EPGProgram, XtreamAccount


def _scalar_one(value):
    class R:
        def scalar_one_or_none(self):
            return value
    return R()


def _make_account(account_id=1):
    return XtreamAccount(
        id=account_id,
        name="Provider",
        server_url="http://example",
        username="user",
        password="",
    )


class DeleteAccountEPGCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def _run_delete(self, account, app_settings=None):
        if app_settings is None:
            app_settings = AppSettings()
        session = AsyncMock()
        # Execute call order:
        #   1. select(XtreamAccount)  -> account lookup
        #   2. select(AppSettings)    -> settings lookup
        #   3. delete(EPGProgram)     -> bulk EPG cleanup (return value unused)
        session.execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(app_settings),
            AsyncMock(),
        ])
        result = await delete_account(account.id, None, session)
        return session, result

    async def test_epg_programs_delete_executed(self):
        account = _make_account()
        session, result = await self._run_delete(account)
        self.assertEqual(result, {"status": "deleted"})
        # Third execute call must target epg_programs table.
        calls = session.execute.await_args_list
        self.assertEqual(len(calls), 3)
        epg_stmt = calls[2].args[0]
        self.assertEqual(str(epg_stmt.table), str(EPGProgram.__table__))

    async def test_epg_delete_runs_before_account_row_delete(self):
        """EPGProgram cleanup must precede session.delete(account)."""
        account = _make_account()
        call_order = []

        orig_execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(AppSettings()),
            AsyncMock(),
        ])

        async def tracking_execute(stmt, *args, **kwargs):
            call_order.append("execute")
            return await orig_execute(stmt, *args, **kwargs)

        async def tracking_delete(obj):
            call_order.append("delete")

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=tracking_execute)
        session.delete = AsyncMock(side_effect=tracking_delete)

        await delete_account(account.id, None, session)

        epg_execute_pos = [i for i, v in enumerate(call_order) if v == "execute"][2]
        delete_pos = next(i for i, v in enumerate(call_order) if v == "delete")
        self.assertLess(epg_execute_pos, delete_pos)

    async def test_session_delete_and_commit_called(self):
        account = _make_account()
        session, _ = await self._run_delete(account)
        session.delete.assert_awaited_with(account)
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
