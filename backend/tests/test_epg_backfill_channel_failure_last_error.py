"""
Regression test: per-channel backfill failure must not set last_error.

When a single channel's get_epg call raises (e.g. network timeout), the
_backfill_from_api loop logs a warning and continues to the next channel.
last_error must stay None so the Settings UI does not show a false
"Guide sync failed" banner for an otherwise successful refresh.

Before fix (the removed line at epg_ingest_manager.py:922):
    except Exception as exc:
        self._status["last_error"] = f"EPG fetch failed for channel {stream_id}: {exc}"
        ...
        continue
A provider with one flaky channel set last_error on every refresh cycle,
showing the alert even though the XMLTV guide updated correctly.

After fix: the except block only logs a warning and continues; last_error
is only set by the outer _refresh_account raise path.
"""
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from unittest.mock import patch

from services.epg_ingest_manager import EPGIngestManager


def _make_session():
    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=MagicMock())

    @asynccontextmanager
    async def fake_begin():
        yield

    fake_session.begin = fake_begin

    @asynccontextmanager
    async def maker():
        yield fake_session

    return maker


class BackfillChannelFailureLastErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_flaky_channel_get_epg_does_not_set_last_error(self):
        """get_epg raising for one channel must leave last_error as None."""
        manager = EPGIngestManager()
        manager._status["last_error"] = None

        client = AsyncMock()
        client.get_epg = AsyncMock(side_effect=ConnectionError("provider timeout"))

        channel_targets = [
            (
                {"stream_id": "101", "name": "BBC HD", "tv_archive": 1},
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                7,
            )
        ]
        now_utc = datetime(2024, 1, 2, tzinfo=timezone.utc)

        with patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session()):
            await manager._backfill_from_api(
                client=client,
                channel_targets=channel_targets,
                now_utc=now_utc,
                processed=0,
                inserted=0,
                account_id=1,
                insert_stmt=MagicMock(),
            )

        self.assertIsNone(
            manager._status["last_error"],
            f"last_error should stay None after per-channel get_epg failure, "
            f"got: {manager._status['last_error']!r}",
        )

    async def test_all_channels_fail_get_epg_does_not_set_last_error(self):
        """All channels failing get_epg must still leave last_error as None."""
        manager = EPGIngestManager()
        manager._status["last_error"] = None

        client = AsyncMock()
        client.get_epg = AsyncMock(side_effect=TimeoutError("timeout"))

        channel_targets = [
            (
                {"stream_id": str(i), "name": f"Ch {i}", "tv_archive": 1},
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                7,
            )
            for i in range(1, 4)
        ]
        now_utc = datetime(2024, 1, 2, tzinfo=timezone.utc)

        with patch("services.epg_ingest_manager.async_session_maker", side_effect=_make_session()):
            await manager._backfill_from_api(
                client=client,
                channel_targets=channel_targets,
                now_utc=now_utc,
                processed=0,
                inserted=0,
                account_id=1,
                insert_stmt=MagicMock(),
            )

        self.assertIsNone(
            manager._status["last_error"],
            f"last_error should stay None after all channels fail get_epg, "
            f"got: {manager._status['last_error']!r}",
        )


if __name__ == "__main__":
    unittest.main()
