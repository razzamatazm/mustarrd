"""
Regression tests: Browse EPG and Catchup endpoints fetched the provider's
entire channel list twice per request.

Both handlers call epg_service.get_channel_archive_days (which fetches the
full channel list via get_live_streams) to cap days_back, and then
get_epg_for_channel / get_past_programs resolved the archive window AGAIN
internally — a second full channel-list fetch on every Browse EPG or Catchup
request. On large providers that is two multi-MB provider calls per click.

Fix: the handlers pass the already-resolved archive_days through, and
get_epg_for_channel only performs the internal lookup when the caller did
not supply one.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, EPGProgram, XtreamAccount
from services.epg_service import EPGService


def _make_account():
    account = MagicMock(spec=XtreamAccount)
    account.id = 1
    account.guide_offset_hours = 0
    return account


def _make_settings():
    settings = MagicMock(spec=AppSettings)
    settings.epg_offset_minutes = 0
    return settings


def _make_session(account, settings):
    async def execute(stmt):
        entity = stmt.column_descriptions[0]["entity"]
        result = MagicMock()
        if entity is XtreamAccount:
            result.scalar_one_or_none.return_value = account
        elif entity is AppSettings:
            result.scalar_one_or_none.return_value = settings
        else:  # EPGProgram query: no stored guide rows, forces the live path
            result.scalars.return_value.all.return_value = []
        return result

    session = AsyncMock()
    session.execute = execute
    return session


def _make_counting_client():
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=[
        {"stream_id": 42, "name": "News HD", "tv_archive": 1, "tv_archive_duration": 7},
    ])
    client.get_epg = AsyncMock(return_value=[])
    client.close = AsyncMock()
    return client


class EndpointSingleChannelListFetchTests(unittest.IsolatedAsyncioTestCase):
    """Each Browse EPG / Catchup request fetches the channel list at most once."""

    async def _run_handler(self, handler, **extra):
        account = _make_account()
        session = _make_session(account, _make_settings())
        client = _make_counting_client()
        service = EPGService()

        with (
            patch("api.channels.epg_service", service),
            patch("services.epg_service.XtreamClient", return_value=client),
            patch("services.epg_service.resolve_account_password_with_migration",
                  AsyncMock(return_value="pass")),
        ):
            result = await handler(
                account_id=1,
                channel_id="42",
                days_back=7,
                _admin=None,
                session=session,
                **extra,
            )
        return result, client

    async def test_browse_epg_fetches_channel_list_once(self):
        from api.channels import get_channel_epg

        _, client = await self._run_handler(get_channel_epg, fresh=False)

        self.assertEqual(
            client.get_live_streams.call_count,
            1,
            "Browse EPG must fetch the provider channel list exactly once per "
            f"request, got {client.get_live_streams.call_count} fetches.",
        )

    async def test_catchup_fetches_channel_list_once(self):
        from api.channels import get_catchup_programs

        _, client = await self._run_handler(get_catchup_programs)

        self.assertEqual(
            client.get_live_streams.call_count,
            1,
            "Catchup must fetch the provider channel list exactly once per "
            f"request, got {client.get_live_streams.call_count} fetches.",
        )


class ServiceArchiveDaysPassThroughTests(unittest.IsolatedAsyncioTestCase):

    async def test_supplied_archive_days_skips_channel_list_lookup(self):
        """get_epg_for_channel(archive_days=...) must not resolve it again."""
        account = _make_account()
        session = _make_session(account, _make_settings())
        client = _make_counting_client()
        service = EPGService()

        with (
            patch("services.epg_service.XtreamClient", return_value=client),
            patch("services.epg_service.resolve_account_password_with_migration",
                  AsyncMock(return_value="pass")),
        ):
            await service.get_epg_for_channel(
                session, 1, "42", days_back=7, archive_days=7
            )

        client.get_live_streams.assert_not_called()

    async def test_without_archive_days_still_resolves_internally(self):
        """Callers that do not pass archive_days keep the old behavior."""
        account = _make_account()
        session = _make_session(account, _make_settings())
        client = _make_counting_client()
        service = EPGService()

        with (
            patch("services.epg_service.XtreamClient", return_value=client),
            patch("services.epg_service.resolve_account_password_with_migration",
                  AsyncMock(return_value="pass")),
        ):
            await service.get_epg_for_channel(session, 1, "42", days_back=7)

        self.assertEqual(client.get_live_streams.call_count, 1)

    async def test_supplied_zero_archive_days_returns_empty(self):
        """archive_days=0 short-circuits like the internal lookup did."""
        account = _make_account()
        session = _make_session(account, _make_settings())
        client = _make_counting_client()
        service = EPGService()

        with (
            patch("services.epg_service.XtreamClient", return_value=client),
            patch("services.epg_service.resolve_account_password_with_migration",
                  AsyncMock(return_value="pass")),
        ):
            result = await service.get_epg_for_channel(
                session, 1, "42", days_back=7, archive_days=0
            )

        self.assertEqual(result, [])
        client.get_epg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
