"""
Regression tests for get_past_programs straddling the archive window boundary.

A program that started before the cutoff but ended within the catchup window
must appear on the Catchup page. The bug used `start_time >= cutoff` which
excluded these programs; the fix uses `end_time > cutoff`.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import EPGService


def _make_program(start_time, end_time, has_archive=True, epg_id="test"):
    return {
        "epg_id": epg_id,
        "title": "Test Program",
        "start_timestamp": int(start_time.timestamp()),
        "stop_timestamp": int(end_time.timestamp()),
        "has_archive": has_archive,
    }


class EPGStradleBoundaryTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.service = EPGService()
        self.session = AsyncMock()

    async def _get_past(self, programs, days_back=7):
        """Call get_past_programs with get_epg_for_channel mocked to return programs."""
        with patch.object(self.service, "get_epg_for_channel", new=AsyncMock(return_value=programs)):
            return await self.service.get_past_programs(
                self.session, account_id=1, channel_id="1", days_back=days_back
            )

    async def test_straddle_program_included(self):
        """
        A 2-hour movie that started 30 minutes before the 7-day cutoff must appear.
        Before the fix, start_time >= cutoff was False so the movie was silently hidden.
        """
        now = datetime.now(timezone.utc)
        # Starts 7 days and 30 minutes ago, ends 7 days and 30 minutes ago + 2 hours
        start = now - timedelta(days=7, minutes=30)
        end = now - timedelta(days=7, minutes=30) + timedelta(hours=2)
        programs = [_make_program(start, end, has_archive=True, epg_id="straddle")]

        result = await self._get_past(programs, days_back=7)

        ids = [p["epg_id"] for p in result]
        self.assertIn("straddle", ids, "Straddle program must be included in catchup results")

    async def test_fully_inside_window_still_included(self):
        """Programs fully within the archive window must continue to appear."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=3)
        end = now - timedelta(days=3) + timedelta(hours=1)
        programs = [_make_program(start, end, has_archive=True, epg_id="inside")]

        result = await self._get_past(programs)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["epg_id"], "inside")

    async def test_entirely_before_cutoff_excluded(self):
        """A program that ended before the cutoff must not appear."""
        now = datetime.now(timezone.utc)
        # Both start and end are before the 7-day cutoff
        start = now - timedelta(days=8, hours=2)
        end = now - timedelta(days=8)
        programs = [_make_program(start, end, has_archive=True, epg_id="old")]

        result = await self._get_past(programs)

        self.assertEqual(result, [], "Program entirely before cutoff must be excluded")

    async def test_currently_airing_excluded(self):
        """A program still in progress (end_time in the future) must not appear."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        programs = [_make_program(start, end, has_archive=True, epg_id="airing")]

        result = await self._get_past(programs)

        self.assertEqual(result, [], "Currently airing program must not appear in catchup list")

    async def test_no_archive_excluded(self):
        """A straddling program with has_archive=False must still be excluded."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7, minutes=30)
        end = start + timedelta(hours=2)
        programs = [_make_program(start, end, has_archive=False, epg_id="no-archive")]

        result = await self._get_past(programs)

        self.assertEqual(result, [], "has_archive=False program must be excluded")

    async def test_mixed_batch(self):
        """Only the straddle and fully-inside programs with has_archive=True appear."""
        now = datetime.now(timezone.utc)
        straddle_start = now - timedelta(days=7, minutes=30)
        straddle_end = straddle_start + timedelta(hours=2)

        inside_start = now - timedelta(days=3)
        inside_end = inside_start + timedelta(hours=1)

        old_start = now - timedelta(days=8, hours=2)
        old_end = now - timedelta(days=8)

        programs = [
            _make_program(straddle_start, straddle_end, has_archive=True, epg_id="straddle"),
            _make_program(inside_start, inside_end, has_archive=True, epg_id="inside"),
            _make_program(old_start, old_end, has_archive=True, epg_id="old"),
        ]

        result = await self._get_past(programs)

        ids = {p["epg_id"] for p in result}
        self.assertIn("straddle", ids)
        self.assertIn("inside", ids)
        self.assertNotIn("old", ids)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
