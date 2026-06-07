import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.accounts import delete_account, list_accounts_public
from api.settings import SettingsUpdate, update_settings
from database import _apply_lightweight_migrations
from models import AppSettings, XtreamAccount


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class DefaultAccountSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_accounts_include_guide_offset_hours(self):
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    all=lambda: [
                        XtreamAccount(
                            id=3,
                            name="Provider A",
                            server_url="http://example",
                            username="user",
                            password="",
                            is_active=True,
                            guide_offset_hours=4,
                        )
                    ]
                )
            )
        )

        result = await list_accounts_public(None, session)

        self.assertEqual(
            result,
            [
                {
                    "id": 3,
                    "name": "Provider A",
                    "is_active": True,
                    "guide_offset_hours": 4,
                }
            ],
        )

    async def test_migration_adds_default_account_id_column_when_missing(self):
        conn = AsyncMock()

        async def column_exists(_conn, table_name, column_name):
            return not (table_name == "app_settings" and column_name == "default_account_id")

        with patch("database._column_exists", side_effect=column_exists):
            await _apply_lightweight_migrations(conn)

        executed = [str(call.args[0]) for call in conn.execute.await_args_list]
        self.assertIn("ALTER TABLE app_settings ADD COLUMN default_account_id INTEGER", executed)

    async def test_update_settings_persists_valid_default_account(self):
        settings = AppSettings()
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _ScalarResult(settings),
                _ScalarResult(XtreamAccount(id=7, name="Primary", server_url="http://example", username="user", password="")),
            ]
        )

        result = await update_settings(
            SettingsUpdate(default_account_id=7),
            None,
            session,
        )

        self.assertEqual(settings.default_account_id, 7)
        self.assertEqual(result["default_account_id"], 7)
        session.commit.assert_awaited()
        session.refresh.assert_awaited_with(settings)

    async def test_update_settings_rejects_unknown_default_account(self):
        settings = AppSettings()
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _ScalarResult(settings),
                _ScalarResult(None),
            ]
        )

        with self.assertRaises(HTTPException) as context:
            await update_settings(
                SettingsUpdate(default_account_id=999),
                None,
                session,
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(context.exception.detail, "Default account not found")

    async def test_delete_account_clears_default_account_reference(self):
        account = XtreamAccount(id=5, name="Primary", server_url="http://example", username="user", password="")
        app_settings = AppSettings()
        app_settings.default_account_id = 5
        _empty_list = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _ScalarResult(account),
                _ScalarResult(app_settings),
                _empty_list,
                _empty_list,
                AsyncMock(),  # delete(EPGProgram)
            ]
        )

        with patch("api.accounts.download_manager"):
            with patch("api.accounts.epg_service"):
                result = await delete_account(5, None, session)

        self.assertIsNone(app_settings.default_account_id)
        self.assertEqual(result, {"status": "deleted"})
        session.delete.assert_awaited_with(account)
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
