"""
Regression test: delete_account must bulk-delete all EPGProgram rows for the
account before removing the account row, and clear the in-process EPG cache so
a reused account id cannot serve stale EPG data.

Before fix: session.delete(account) left epg_programs rows with the deleted
account_id.  Rows accumulated indefinitely, and SQLite integer PK reuse meant
a new account with the same id immediately inherited the old account's EPG data.
The in-memory EPGService cache also kept serving the old data until TTL expired.

After fix: a DELETE FROM epg_programs WHERE account_id = ? statement runs inside
the same transaction before session.delete(account), so no orphaned rows remain.
epg_service.clear_cache() is called after the commit so no stale cache entries
survive account deletion.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _scalars_empty():
    """Return a mock execute result whose .scalars().all() yields []."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    return result


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
        #   1. select(XtreamAccount)    -> account lookup
        #   2. select(AppSettings)      -> settings lookup
        #   3. select(ScheduledRecording) -> schedule cancel (PR #121)
        #   4. select(Download)         -> active download ids (PR #121)
        #   5. delete(EPGProgram)       -> bulk EPG cleanup
        session.execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(app_settings),
            _scalars_empty(),
            _scalars_empty(),
            AsyncMock(),
        ])
        with patch("api.accounts.download_manager"):
            with patch("api.accounts.epg_service"):
                result = await delete_account(account.id, None, session)
        return session, result

    async def test_epg_programs_delete_executed(self):
        account = _make_account()
        session, result = await self._run_delete(account)
        self.assertEqual(result, {"status": "deleted"})
        # Fifth execute call must target epg_programs table.
        calls = session.execute.await_args_list
        self.assertEqual(len(calls), 5)
        epg_stmt = calls[4].args[0]
        self.assertEqual(str(epg_stmt.table), str(EPGProgram.__table__))

    async def test_epg_delete_runs_before_account_row_delete(self):
        """EPGProgram cleanup must precede session.delete(account)."""
        account = _make_account()
        call_order = []

        orig_execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(AppSettings()),
            _scalars_empty(),
            _scalars_empty(),
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

        with patch("api.accounts.download_manager"):
            with patch("api.accounts.epg_service"):
                await delete_account(account.id, None, session)

        # The 5th execute (index 4) is the EPG delete; it must precede session.delete
        epg_execute_pos = [i for i, v in enumerate(call_order) if v == "execute"][4]
        delete_pos = next(i for i, v in enumerate(call_order) if v == "delete")
        self.assertLess(epg_execute_pos, delete_pos)

    async def test_session_delete_and_commit_called(self):
        account = _make_account()
        session, _ = await self._run_delete(account)
        session.delete.assert_awaited_with(account)
        session.commit.assert_awaited()

    async def test_epg_cache_cleared_on_delete(self):
        """epg_service.clear_cache() must be called after account deletion."""
        account = _make_account()
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(AppSettings()),
            _scalars_empty(),
            _scalars_empty(),
            AsyncMock(),
        ])
        with patch("api.accounts.download_manager"):
            with patch("api.accounts.epg_service") as mock_epg_service:
                await delete_account(account.id, None, session)
                mock_epg_service.clear_cache.assert_called_once()


if __name__ == "__main__":
    unittest.main()
