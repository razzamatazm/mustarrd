"""
Regression test: force refresh wipes the guide when the XMLTV document is malformed.

Bug: on a force refresh, _refresh_account deleted all existing EPGProgram
rows for the account (in its own committed transaction) before the XMLTV
document was ever parsed. The only guard was a raw byte count of
"<programme" occurrences. _iter_programs swallows ET.ParseError, so a
document that contains programme tags but is truncated or malformed (a
common failure mode when the provider serves a half-written file) wiped
the whole guide and re-imported only the entries before the parse error —
possibly none. The lost rows could not be restored until the provider
served a good file again.

Fix: _scan_channel_map pre-parses the document before the delete and
reports whether it parses to the end. On a force refresh with a malformed
document, the delete is skipped (a warning is logged) and whatever parses
is merged into the existing guide via the upsert instead.
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
    account.last_epg_backfill_at = None
    return account


def _make_xtream_client(xmltv_bytes=b""):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=[])
    client.get_xmltv = AsyncMock(return_value=xmltv_bytes)
    client.get_epg = AsyncMock(return_value=[])
    client.close = AsyncMock()
    return client


def _make_session_ctx(delete_tracker):
    """Return a fake async_session_maker that records DELETE statements."""

    fake_session = AsyncMock()

    async def tracking_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        if "DELETE" in stmt_str.upper():
            delete_tracker.append(stmt_str)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.all.return_value = []
        return result

    fake_session.execute = tracking_execute

    @asynccontextmanager
    async def fake_begin():
        yield

    fake_session.begin = fake_begin

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker


# Contains two <programme tags (so the raw byte count is > 0) but the
# document is truncated mid-attribute: ET.iterparse raises ParseError.
_TRUNCATED_XMLTV = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<channel id="ch1"><display-name>Test</display-name></channel>'
    b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="ch1">'
    b"<title>Show One</title>"
    b"</programme>"
    b'<programme start="20260608130000 +0000" stop="20260608'
)


class ForceRefreshMalformedXmltvTests(unittest.IsolatedAsyncioTestCase):

    async def test_force_refresh_malformed_xmltv_does_not_delete_rows(self):
        """No DELETE issued on force refresh when the XMLTV document is
        truncated, even though it contains <programme tags."""
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        client = _make_xtream_client(xmltv_bytes=_TRUNCATED_XMLTV)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx(delete_tracker)),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=True)

        self.assertEqual(
            delete_tracker,
            [],
            "No DELETE must be issued on force refresh when the XMLTV document "
            "fails to parse to the end; the old guide rows are the only copy of "
            f"the data. Got DELETE statements: {delete_tracker}",
        )

    async def test_force_refresh_well_formed_xmltv_still_deletes_rows(self):
        """Regression guard: a force refresh with a well-formed document still
        clears the existing rows before the reload."""
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        well_formed = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b"<tv>"
            b'<channel id="ch1"><display-name>Test</display-name></channel>'
            b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="ch1">'
            b"<title>Show One</title>"
            b"</programme>"
            b"</tv>"
        )
        client = _make_xtream_client(xmltv_bytes=well_formed)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx(delete_tracker)),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=True)

        self.assertTrue(
            len(delete_tracker) >= 1,
            "A force refresh with a well-formed XMLTV document must still clear "
            "existing rows before reloading.",
        )


if __name__ == "__main__":
    unittest.main()
