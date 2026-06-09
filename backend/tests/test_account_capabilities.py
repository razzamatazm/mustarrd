"""
Regression tests: provider capabilities summary on the account row.

The EPG ingest already fetches the full live channel list each refresh, so the
catchup capabilities (channel count, max archive days) are derived from that
fetch plus one VOD category lookup, and persisted on the account. The accounts
API then serves them from the row — no live provider call per page load.
"""
import sys
import unittest
from contextlib import asynccontextmanager
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
    account.server_url = "http://provider.example"
    account.username = "user"
    return account


def _make_session(db_account):
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_account))
    )
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_refresh_session_maker():
    """Fake async_session_maker for the main _refresh_account transaction."""
    fake_session = AsyncMock()

    async def fake_execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.all.return_value = []
        return result

    fake_session.execute = fake_execute

    @asynccontextmanager
    async def fake_begin():
        yield

    fake_session.begin = fake_begin

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker


class UpdateProviderCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_capabilities_to_account(self):
        """_update_provider_capabilities persists count, max days and VOD flag."""
        manager = EPGIngestManager()
        db_account = MagicMock()
        session = _make_session(db_account)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_provider_capabilities(
                1,
                catchup_channel_count=1234,
                catchup_max_archive_days=7,
                vod_available=True,
            )

        self.assertEqual(db_account.catchup_channel_count, 1234)
        self.assertEqual(db_account.catchup_max_archive_days, 7)
        self.assertTrue(db_account.vod_available)
        session.commit.assert_awaited()

    async def test_vod_none_keeps_previous_value(self):
        """A failed VOD probe (None) must not overwrite the stored flag."""
        manager = EPGIngestManager()
        db_account = MagicMock()
        db_account.vod_available = True
        session = _make_session(db_account)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_provider_capabilities(
                1,
                catchup_channel_count=10,
                catchup_max_archive_days=3,
                vod_available=None,
            )

        self.assertTrue(db_account.vod_available)
        self.assertEqual(db_account.catchup_channel_count, 10)

    async def test_missing_account_does_not_commit(self):
        """No-op when the account no longer exists."""
        manager = EPGIngestManager()
        session = _make_session(None)

        with patch("services.epg_ingest_manager.async_session_maker", return_value=session):
            await manager._update_provider_capabilities(
                99,
                catchup_channel_count=1,
                catchup_max_archive_days=1,
                vod_available=False,
            )

        session.commit.assert_not_called()


class RefreshAccountCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    def _make_client(self, channels, vod_categories=None, vod_error=None):
        client = MagicMock()
        client.get_live_streams = AsyncMock(return_value=channels)
        client.get_xmltv = AsyncMock(return_value=b"")
        client.close = AsyncMock()
        if vod_error is not None:
            client.get_vod_categories = AsyncMock(side_effect=vod_error)
        else:
            client.get_vod_categories = AsyncMock(return_value=vod_categories or [])
        return client

    async def _run_refresh(self, client):
        manager = EPGIngestManager()
        account = _make_account()
        # Recent backfill keeps the cooldown active so the refresh does not
        # walk into the per-channel API backfill path.
        account.last_epg_backfill_at = datetime.now(timezone.utc)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_refresh_session_maker()),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
            patch.object(manager, "_update_provider_capabilities", new=AsyncMock()) as mock_caps,
            patch.object(manager, "_log", new=AsyncMock()),
        ):
            await manager._refresh_account(account)

        return mock_caps

    async def test_refresh_persists_channel_count_and_max_days(self):
        """Refresh derives count and max archive days from the live channel list."""
        channels = [
            {"stream_id": 1, "name": "A", "tv_archive": 1, "tv_archive_duration": 3},
            {"stream_id": 2, "name": "B", "tv_archive": 1, "tv_archive_duration": 7},
            {"stream_id": 3, "name": "C", "tv_archive": 0, "tv_archive_duration": 0},
        ]
        client = self._make_client(channels, vod_categories=[{"category_id": "1"}])

        mock_caps = await self._run_refresh(client)

        mock_caps.assert_awaited_once()
        call = mock_caps.call_args
        self.assertEqual(call.args[0], 1)
        self.assertEqual(call.kwargs["catchup_channel_count"], 2)
        self.assertEqual(call.kwargs["catchup_max_archive_days"], 7)
        self.assertTrue(call.kwargs["vod_available"])

    async def test_refresh_empty_vod_categories_reports_unavailable(self):
        """A provider with no VOD categories is recorded as vod_available=False."""
        channels = [
            {"stream_id": 1, "name": "A", "tv_archive": 1, "tv_archive_duration": 2},
        ]
        client = self._make_client(channels, vod_categories=[])

        mock_caps = await self._run_refresh(client)

        mock_caps.assert_awaited_once()
        self.assertFalse(mock_caps.call_args.kwargs["vod_available"])

    async def test_refresh_vod_probe_failure_does_not_break_refresh(self):
        """A failing VOD lookup passes None (keep previous) and the refresh continues."""
        channels = [
            {"stream_id": 1, "name": "A", "tv_archive": 1, "tv_archive_duration": 2},
        ]
        client = self._make_client(channels, vod_error=Exception("boom"))

        mock_caps = await self._run_refresh(client)

        mock_caps.assert_awaited_once()
        self.assertIsNone(mock_caps.call_args.kwargs["vod_available"])
        self.assertEqual(mock_caps.call_args.kwargs["catchup_channel_count"], 1)


class AccountToDictCapabilitiesTests(unittest.TestCase):
    def _make_mock_account(self):
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
        account.last_connection_checked_at = None
        account.last_connection_error = None
        account.catchup_channel_count = 1234
        account.catchup_max_archive_days = 7
        account.vod_available = True
        return account

    def test_to_dict_includes_capability_fields(self):
        """The accounts payload carries the capabilities so the UI needs no live call."""
        d = XtreamAccount.to_dict(self._make_mock_account())
        self.assertEqual(d["catchup_channel_count"], 1234)
        self.assertEqual(d["catchup_max_archive_days"], 7)
        self.assertTrue(d["vod_available"])

    def test_to_dict_capability_fields_default_none(self):
        """Unknown capabilities serialize as None, not 0/False."""
        account = self._make_mock_account()
        account.catchup_channel_count = None
        account.catchup_max_archive_days = None
        account.vod_available = None
        d = XtreamAccount.to_dict(account)
        self.assertIsNone(d["catchup_channel_count"])
        self.assertIsNone(d["catchup_max_archive_days"])
        self.assertIsNone(d["vod_available"])


if __name__ == "__main__":
    unittest.main()
