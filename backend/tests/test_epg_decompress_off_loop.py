"""
Regression test: XMLTV decompression must not block the event loop.

_refresh_account called self._maybe_decompress(raw_xmltv) synchronously on
the event loop. Decompressing a large provider guide (tens of MB of gzip)
is CPU-bound and stalled every download, WebSocket update, and API request
for the duration.

Fix: the call site offloads the decompression via asyncio.to_thread.
_maybe_decompress itself stays synchronous (it is unit-tested directly
elsewhere); this test asserts the refresh pipeline runs it off-loop.
"""
import gzip
import sys
import threading
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


def _make_xtream_client(xmltv_bytes):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=[])
    client.get_xmltv = AsyncMock(return_value=xmltv_bytes)
    client.close = AsyncMock()
    return client


def _make_session_maker():
    fake_session = AsyncMock()

    async def execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.all.return_value = []
        return result

    fake_session.execute = execute

    @asynccontextmanager
    async def fake_begin():
        yield

    fake_session.begin = fake_begin

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker


class EPGDecompressOffLoopTests(unittest.IsolatedAsyncioTestCase):

    async def test_gzip_decompression_runs_off_the_event_loop_thread(self):
        manager = EPGIngestManager()
        account = _make_account()
        loop_thread = threading.current_thread()
        seen_threads = []

        gzipped = gzip.compress(b'<?xml version="1.0"?><tv></tv>')
        client = _make_xtream_client(gzipped)

        original_decompress = gzip.decompress

        def recording_decompress(data):
            seen_threads.append(threading.current_thread())
            return original_decompress(data)

        with (
            patch("services.epg_ingest_manager.async_session_maker",
                  side_effect=_make_session_maker()),
            patch("services.epg_ingest_manager.resolve_account_password",
                  return_value="pass"),
            patch("services.epg_ingest_manager.XtreamClient", return_value=client),
            patch("services.epg_ingest_manager.gzip.decompress", recording_decompress),
            patch.object(manager, "_should_backfill", return_value=False),
        ):
            await manager._refresh_account(account, force=False)

        self.assertEqual(
            len(seen_threads), 1,
            "gzip.decompress must be called exactly once for a gzipped guide",
        )
        self.assertIsNot(
            seen_threads[0],
            loop_thread,
            "XMLTV gzip decompression ran on the event-loop thread; a large "
            "guide would stall downloads and WebSocket updates.",
        )


if __name__ == "__main__":
    unittest.main()
