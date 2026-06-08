"""
Regression test: force-refresh EPG wipes entire guide when provider returns empty XMLTV body.

Before the fix:
- _refresh_account committed the force-delete in its own transaction before checking
  whether xmltv_bytes was non-empty.
- Provider returning 200 OK with empty body deleted all EPGProgram rows and inserted
  nothing. Guide empty for up to 8 hours.

After the fix:
- The force-delete is guarded by `if total_programs:`. An XMLTV response with no
  <programme> tags (empty body or well-formed but empty document) leaves existing rows
  intact and logs a warning.
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
    return account


def _make_xtream_client(xmltv_bytes=b""):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=[])
    client.get_xmltv = AsyncMock(return_value=xmltv_bytes)
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


class EPGForceWipeEmptyXmltvTests(unittest.IsolatedAsyncioTestCase):

    async def test_force_refresh_empty_xmltv_does_not_delete_rows(self):
        """No DELETE issued when provider returns empty XMLTV body with force=True."""
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        client = _make_xtream_client(xmltv_bytes=b"")

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx(delete_tracker)),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=True)

        self.assertEqual(
            delete_tracker,
            [],
            "No DELETE must be issued when XMLTV body is empty. "
            f"Got DELETE statements: {delete_tracker}",
        )

    async def test_force_refresh_well_formed_but_programmeless_xmltv_does_not_delete_rows(self):
        """No DELETE issued when provider returns valid XML with no <programme> tags.

        A mid-regeneration or throttled provider may return a well-formed <tv></tv>
        document with zero programme entries. The guard keys off parsed programme count,
        not raw byte length, so existing rows are preserved in this case too.
        """
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        empty_but_valid_xmltv = b'<?xml version="1.0" encoding="UTF-8"?><tv></tv>'
        client = _make_xtream_client(xmltv_bytes=empty_but_valid_xmltv)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx(delete_tracker)),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=True)

        self.assertEqual(
            delete_tracker,
            [],
            "No DELETE must be issued when XMLTV has no <programme> tags (empty but valid document). "
            f"Got DELETE statements: {delete_tracker}",
        )

    async def test_force_refresh_nonempty_xmltv_does_delete_rows(self):
        """DELETE is issued when provider returns an XMLTV body with programme entries."""
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        xmltv_with_programme = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<tv>'
            b'<programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="ch1">'
            b'<title>Test Show</title>'
            b'</programme>'
            b'</tv>'
        )
        client = _make_xtream_client(xmltv_bytes=xmltv_with_programme)

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session_ctx(delete_tracker)),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            await manager._refresh_account(account, force=True)

        self.assertTrue(
            len(delete_tracker) >= 1,
            "A DELETE must be issued when XMLTV body contains <programme> entries and force=True.",
        )


if __name__ == "__main__":
    unittest.main()
