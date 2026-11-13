"""
The per-account catchup URL style reaches the URL the recording actually uses,
and the settings API only accepts the three values it means.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pydantic

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.accounts import AccountCreate, AccountUpdate, update_account
from api.channels import _resolve_preview_stream_url
from database import _apply_lightweight_migrations
from models import AppSettings, XtreamAccount
from services.download_builder import build_download_from_program

PROGRAM = {
    "title": "Show",
    "start_time": "2026-03-30T21:00:00+00:00",
    "end_time": "2026-03-30T22:00:00+00:00",
    "start_timestamp": 1774904400,
    "stop_timestamp": 1774908000,
    "provider_start": "2026-03-30:21-00",
}


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _account(style="auto", resolved=None):
    account = XtreamAccount(
        id=1,
        name="Provider",
        server_url="https://provider.example.com",
        username="user",
        password="",
    )
    account.catchup_url_style = style
    account.catchup_url_style_resolved = resolved
    return account


async def _build(account):
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_ScalarResult(account), _ScalarResult(AppSettings(download_folder="/tmp"))]
    )
    with (
        patch(
            "services.download_builder.resolve_account_password_with_migration",
            new=AsyncMock(return_value="pass"),
        ),
        patch("services.output_path.file_namer.generate_filename", return_value="test.ts"),
        patch("services.download_builder.epg_service.detect_program_type", return_value="other"),
    ):
        return await build_download_from_program(
            session,
            account_id=account.id,
            channel_id="999",
            channel_name="Test Channel",
            program=PROGRAM,
        )


class DownloadUrlFollowsAccountStyleTests(unittest.TestCase):
    def test_default_auto_account_still_gets_the_path_form(self):
        download = asyncio.run(_build(_account()))
        self.assertEqual(
            download.source_url,
            "https://provider.example.com/timeshift/user/pass/60/2026-03-30:21-00/999.ts",
        )

    def test_forced_query_account_gets_the_query_form(self):
        download = asyncio.run(_build(_account(style="query")))
        self.assertEqual(
            download.source_url,
            "https://provider.example.com/streaming/timeshift.php"
            "?username=user&password=pass&stream=999&start=2026-03-30:21-00&duration=60",
        )

    def test_auto_account_uses_the_style_probing_resolved(self):
        download = asyncio.run(_build(_account(resolved="query")))
        self.assertIn("/streaming/timeshift.php", download.source_url)

    def test_forced_path_overrides_a_resolved_query(self):
        download = asyncio.run(_build(_account(style="path", resolved="query")))
        self.assertIn("/timeshift/", download.source_url)
        self.assertNotIn("timeshift.php", download.source_url)


class AccountCreateValidationTests(unittest.TestCase):
    """The add-account form sends the picker too, so create has to honour it."""

    def _create(self, **fields):
        return AccountCreate(
            name="P", server_url="http://p", username="u", password="p", **fields
        )

    def test_a_new_account_defaults_to_auto(self):
        self.assertEqual(self._create().catchup_url_style, "auto")

    def test_a_new_account_can_be_pinned_at_creation(self):
        self.assertEqual(self._create(catchup_url_style="query").catchup_url_style, "query")

    def test_anything_else_is_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            self._create(catchup_url_style="sideways")


class AccountUpdateValidationTests(unittest.TestCase):
    def test_the_three_values_are_accepted(self):
        for value in ("auto", "path", "query"):
            self.assertEqual(AccountUpdate(catchup_url_style=value).catchup_url_style, value)

    def test_case_and_whitespace_are_normalised(self):
        self.assertEqual(AccountUpdate(catchup_url_style=" QUERY ").catchup_url_style, "query")

    def test_anything_else_is_rejected(self):
        for value in ("", "auto ish", "http", "PATHS"):
            with self.assertRaises(pydantic.ValidationError):
                AccountUpdate(catchup_url_style=value)

    def test_omitting_the_field_leaves_it_unset(self):
        self.assertNotIn("catchup_url_style", AccountUpdate(name="x").model_dump(exclude_unset=True))


class AccountToDictTests(unittest.TestCase):
    def test_both_fields_are_exposed_to_the_frontend(self):
        payload = _account(style="auto", resolved="query").to_dict()
        self.assertEqual(payload["catchup_url_style"], "auto")
        self.assertEqual(payload["catchup_url_style_resolved"], "query")

    def test_a_legacy_row_with_no_style_reads_as_auto(self):
        payload = _account(style=None).to_dict()
        self.assertEqual(payload["catchup_url_style"], "auto")
        self.assertIsNone(payload["catchup_url_style_resolved"])


class PreviewUrlFollowsAccountStyleTests(unittest.IsolatedAsyncioTestCase):
    """A preview of a query-only provider has to use the query form too."""

    async def _preview(self, account):
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_ScalarResult(account))
        with patch(
            "api.channels.resolve_account_password_with_migration",
            new=AsyncMock(return_value="pass"),
        ):
            return await _resolve_preview_stream_url(
                session,
                account_id=1,
                channel_id="999",
                mode="catchup",
                start_timestamp=1774904400,
                stop_timestamp=1774908000,
                provider_start="2026-03-30:21-00",
            )

    async def test_auto_account_previews_with_the_path_form(self):
        url = await self._preview(_account())
        self.assertIn("/timeshift/", url)
        self.assertNotIn("timeshift.php", url)

    async def test_forced_query_account_previews_with_the_query_form(self):
        url = await self._preview(_account(style="query"))
        self.assertIn("/streaming/timeshift.php?", url)

    async def test_resolved_query_style_is_used_for_previews(self):
        url = await self._preview(_account(resolved="query"))
        self.assertIn("/streaming/timeshift.php?", url)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_columns_are_added_when_missing(self):
        conn = AsyncMock()

        async def column_exists(_conn, table_name, column_name):
            return not (
                table_name == "xtream_accounts"
                and column_name in ("catchup_url_style", "catchup_url_style_resolved")
            )

        with patch("database._column_exists", side_effect=column_exists):
            await _apply_lightweight_migrations(conn)

        executed = [str(call.args[0]) for call in conn.execute.await_args_list]
        self.assertIn(
            "ALTER TABLE xtream_accounts ADD COLUMN catchup_url_style VARCHAR(16) DEFAULT 'auto'",
            executed,
        )
        self.assertIn(
            "ALTER TABLE xtream_accounts ADD COLUMN catchup_url_style_resolved VARCHAR(16)",
            executed,
        )


class UpdateAccountStyleTests(unittest.IsolatedAsyncioTestCase):
    async def _update(self, account, **fields):
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_ScalarResult(account))
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        return await update_account(1, AccountUpdate(**fields), session=session)

    async def test_changing_the_setting_forgets_the_resolved_style(self):
        account = _account(style="auto", resolved="query")
        await self._update(account, catchup_url_style="path")
        self.assertEqual(account.catchup_url_style, "path")
        self.assertIsNone(account.catchup_url_style_resolved)

    async def test_resaving_the_same_setting_keeps_the_resolved_style(self):
        account = _account(style="auto", resolved="query")
        await self._update(account, catchup_url_style="auto")
        self.assertEqual(account.catchup_url_style_resolved, "query")

    async def test_updating_other_fields_leaves_the_style_alone(self):
        account = _account(style="auto", resolved="query")
        await self._update(account, name="Renamed")
        self.assertEqual(account.catchup_url_style, "auto")
        self.assertEqual(account.catchup_url_style_resolved, "query")


if __name__ == "__main__":
    unittest.main()
