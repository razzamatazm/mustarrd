"""
Regression test: backfill marked complete even when the provider API fails entirely.

Bug: _refresh_account always called _mark_backfill_attempt after
_backfill_from_api returned, even when every per-channel get_epg call had
raised (a total provider API failure: provider down, auth expired, network
out). Each failure is caught inside the loop with a warning + continue, so
the loop "completes" without fetching anything. Setting
last_epg_backfill_at then started the backfill cooldown (max(refresh
interval, 6 hours)), so the EPG gaps the backfill was supposed to fill
stayed empty for 6+ hours even if the provider recovered within minutes.

Fix: _backfill_from_api reports whether every attempted fetch failed; on
total failure _refresh_account logs a warning and skips
_mark_backfill_attempt so the next refresh cycle retries the backfill.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


def _make_account(account_id=1):
    account = MagicMock()
    account.id = account_id
    account.name = "Test Account"
    account.server_url = "http://provider.example"
    account.username = "user"
    account.last_epg_backfill_at = None  # no cooldown: backfill should run
    return account


def _make_xtream_client(get_epg):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=[
        {"stream_id": 101, "name": "CNN", "tv_archive": 1, "tv_archive_duration": 7},
    ])
    client.get_xmltv = AsyncMock(return_value=b"")
    client.get_epg = get_epg
    client.close = AsyncMock()
    return client


def _make_session_ctx():
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


class BackfillTotalFailureNotMarkedTests(unittest.IsolatedAsyncioTestCase):

    async def _run_refresh(self, get_epg):
        manager = EPGIngestManager()
        manager._mark_backfill_attempt = AsyncMock()
        account = _make_account()
        client = _make_xtream_client(get_epg)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx()),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=False)

        return manager

    async def test_total_api_failure_does_not_mark_backfill_complete(self):
        """When every get_epg call raises, the backfill must not be marked
        complete; otherwise the cooldown blocks a retry for 6+ hours."""
        manager = await self._run_refresh(
            AsyncMock(side_effect=ConnectionError("provider down"))
        )
        manager._mark_backfill_attempt.assert_not_awaited()

    async def test_successful_backfill_is_marked_complete(self):
        """Regression guard: a backfill where the provider responds (even with
        no entries) still records the attempt so the cooldown applies."""
        manager = await self._run_refresh(AsyncMock(return_value=[]))
        manager._mark_backfill_attempt.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
