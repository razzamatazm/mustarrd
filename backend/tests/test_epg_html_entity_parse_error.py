"""
Regression tests: HTML entities in XMLTV descriptions abort parse after force-mode row deletion.

ET.iterparse (expat) rejects HTML entities not defined in the XML spec: &nbsp;,
&eacute;, &mdash;, etc. Providers that generate XMLTV from HTML often include these.

When _iter_programs hits such an entity it raises xml.etree.ElementTree.ParseError.
No try/except wraps the iteration loop at line 345 of _ingest_account_epg.

With force=True, the sequence is:
  1. total_programs = xmltv_bytes.count(b"<programme") > 0 (entity is in description,
     but the <programme> element is real and counted).
  2. Force-delete commits: all EPGProgram rows for the account wiped.
  3. ET.iterparse raises ParseError when it encounters &nbsp; in the description.
  4. Exception propagates out of _refresh_account (no catch for ParseError there).
  5. Caller (_refresh_all_accounts) catches and logs. No rows inserted.
  6. EPG is empty.

Expected after fix:
  - _refresh_account does not raise (ParseError caught and handled internally).
  - Rows parsed before the bad entity are kept; guide not completely wiped.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import xml.etree.ElementTree as ET

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager

# XMLTV with a real <programme> before a programme that contains &nbsp; in desc.
# The first programme can be counted and parsed; the second causes ParseError.
_XMLTV_WITH_HTML_ENTITY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<channel id="ch1"><display-name>Test Channel</display-name></channel>'
    b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="ch1">'
    b"<title>Good Show</title>"
    b"</programme>"
    b'<programme start="20261201210000 +0000" stop="20261201220000 +0000" channel="ch1">'
    b"<title>Bad Show</title>"
    b"<desc>Watch it&nbsp;on Monday</desc>"
    b"</programme>"
    b"</tv>"
)


def _make_account(account_id=1):
    account = MagicMock()
    account.id = account_id
    account.name = "Test"
    account.server_url = "http://provider.example"
    account.username = "user"
    return account


def _make_session_ctx(delete_tracker):
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


class EPGHtmlEntityParseErrorTests(unittest.IsolatedAsyncioTestCase):

    async def test_html_entity_in_desc_does_not_propagate_parse_error(self):
        """_refresh_account must not raise ParseError when XMLTV contains &nbsp;."""
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []

        client = MagicMock()
        client.get_live_streams = AsyncMock(return_value=[
            {
                "stream_id": "ch1",
                "name": "Test Channel",
                "tv_archive": 1,
                "tv_archive_duration": 7,
                "epg_channel_id": "ch1",
            }
        ])
        client.get_xmltv = AsyncMock(return_value=_XMLTV_WITH_HTML_ENTITY)
        client.close = AsyncMock()

        with (
            patch(
                "services.epg_ingest_manager.async_session_maker",
                side_effect=_make_session_ctx(delete_tracker),
            ),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            try:
                await manager._refresh_account(account, force=True)
            except ET.ParseError as exc:
                self.fail(
                    f"ParseError from &nbsp; in XMLTV must not propagate out of "
                    f"_refresh_account. Got: {exc}"
                )

    async def test_html_entity_force_mode_does_not_wipe_rows_on_parse_error(self):
        """Force-mode must not delete rows when the parse subsequently fails.

        Current bug: delete commits before iteration; ParseError then leaves guide empty.
        After fix: either defer deletion until after successful parse, or catch ParseError
        and keep partial results (no total wipe).
        """
        manager = EPGIngestManager()
        account = _make_account()
        delete_tracker = []
        insert_tracker = []

        fake_session = AsyncMock()

        async def tracking_execute(stmt, *args, **kwargs):
            stmt_str = str(stmt)
            if "DELETE" in stmt_str.upper():
                delete_tracker.append(stmt_str)
            elif "INSERT" in stmt_str.upper():
                insert_tracker.append(stmt_str)
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

        client = MagicMock()
        client.get_live_streams = AsyncMock(return_value=[
            {
                "stream_id": "ch1",
                "name": "Test Channel",
                "tv_archive": 1,
                "tv_archive_duration": 7,
                "epg_channel_id": "ch1",
            }
        ])
        client.get_xmltv = AsyncMock(return_value=_XMLTV_WITH_HTML_ENTITY)
        client.close = AsyncMock()

        with (
            patch("services.epg_ingest_manager.async_session_maker", side_effect=maker),
            patch("services.epg_ingest_manager.resolve_account_password", return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
        ):
            try:
                await manager._refresh_account(account, force=True)
            except ET.ParseError:
                pass  # Bug present: parse error propagated

        # After fix: if delete fired but parse failed, this should be impossible:
        # either no delete (deferred until post-parse) or partial rows still inserted.
        if delete_tracker:
            self.assertGreater(
                len(insert_tracker),
                0,
                "Force-delete fired but ParseError prevented any rows being inserted. "
                "EPG wiped completely. Delete must be deferred until after a successful parse, "
                "or partial results must be committed before the failing element.",
            )


if __name__ == "__main__":
    unittest.main()
