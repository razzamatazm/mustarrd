import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models.account import XtreamAccount

from services.epg_ingest_manager import EPGIngestManager


def _make_account(account_id=1):
    account = MagicMock()
    account.id = account_id
    account.name = "Test Provider"
    return account


class ConnectionStatusUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_sets_last_connection_ok_false(self):
        """EPG refresh failure writes last_connection_ok=False to the account."""
        manager = EPGIngestManager()
        db_account = MagicMock()
        db_account.last_connection_ok = None
        db_account.last_connection_checked_at = None

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_account)))
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_connection_status(account_id=1, ok=False)

        self.assertFalse(db_account.last_connection_ok)
        self.assertIsNotNone(db_account.last_connection_checked_at)

    async def test_failure_stores_error_message(self):
        """_update_connection_status stores the error string when ok=False."""
        manager = EPGIngestManager()
        db_account = MagicMock()

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_account)))
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_connection_status(account_id=1, ok=False, error="Invalid credentials. Check your username and password.")

        self.assertEqual(db_account.last_connection_error, "Invalid credentials. Check your username and password.")

    async def test_success_clears_error_message(self):
        """_update_connection_status clears last_connection_error when ok=True."""
        manager = EPGIngestManager()
        db_account = MagicMock()
        db_account.last_connection_error = "Previous error"

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_account)))
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_connection_status(account_id=1, ok=True)

        self.assertIsNone(db_account.last_connection_error)

    async def test_success_sets_last_connection_ok_true(self):
        """EPG refresh success writes last_connection_ok=True to the account."""
        manager = EPGIngestManager()
        db_account = MagicMock()

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_account)))
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_connection_status(account_id=1, ok=True)

        self.assertTrue(db_account.last_connection_ok)
        self.assertIsNotNone(db_account.last_connection_checked_at)

    async def test_missing_account_does_not_raise(self):
        """_update_connection_status is a no-op when the account no longer exists."""
        manager = EPGIngestManager()

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_connection_status(account_id=99, ok=False)

        session.commit.assert_not_called()

    async def test_refresh_all_accounts_marks_failure_on_exception(self):
        """_refresh_all_accounts calls _update_connection_status(ok=False) when _refresh_account raises."""
        manager = EPGIngestManager()
        account = _make_account(1)

        with (
            patch.object(manager, "_refresh_account", new=AsyncMock(side_effect=Exception("timeout"))),
            patch.object(manager, "_update_connection_status", new=AsyncMock()) as mock_status,
            patch.object(manager, "_log", new=AsyncMock()),
            patch("services.epg_ingest_manager.async_session_maker") as mock_sm,
        ):
            session = AsyncMock()
            session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[account])))))
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            mock_sm.return_value = session

            await manager._refresh_all_accounts()

        mock_status.assert_called_once()
        call = mock_status.call_args
        self.assertEqual(call.args[0], account.id)
        self.assertFalse(call.kwargs["ok"])
        self.assertIsNotNone(call.kwargs.get("error"))


class AccountToDictTimestampTests(unittest.TestCase):
    def _make_mock_account(self, checked_at):
        account = MagicMock(spec=XtreamAccount)
        account.id = 1
        account.name = "Test"
        account.server_url = "http://example.com"
        account.username = "user"
        account.is_active = True
        account.created_at = datetime(2026, 6, 6, 23, 0, 0)
        account.last_used = None
        account.last_epg_backfill_at = None
        account.max_connections = None
        account.active_connections = None
        account.expiration_date = None
        account.guide_offset_hours = 0
        account.last_connection_ok = True
        account.last_connection_checked_at = checked_at
        return account

    def test_last_connection_checked_at_emits_utc_z_suffix(self):
        # Naive datetimes stored by SQLite must be emitted with a Z suffix so
        # the browser parses them as UTC rather than local time.
        account = self._make_mock_account(datetime(2026, 6, 6, 23, 0, 0))
        d = XtreamAccount.to_dict(account)
        self.assertTrue(
            d["last_connection_checked_at"].endswith("Z"),
            f"Expected UTC Z suffix, got: {d['last_connection_checked_at']}"
        )

    def test_last_connection_checked_at_none_stays_none(self):
        account = self._make_mock_account(None)
        d = XtreamAccount.to_dict(account)
        self.assertIsNone(d["last_connection_checked_at"])


if __name__ == "__main__":
    unittest.main()
