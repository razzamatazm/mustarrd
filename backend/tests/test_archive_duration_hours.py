"""
Regression tests: tv_archive_duration reported in hours must not inflate the
catchup window 24x.

Most Xtream providers report tv_archive_duration in days (e.g. 7), but some
send hours (e.g. 168 for a 7-day archive). The old code treated the value as
days unconditionally, so an hours-provider produced a 168-day window: EPG
ingest kept months of stale programs, the channel list advertised "168 days
catchup", and the scheduler dispatched downloads that 404'd at the provider.

The shared helper EPGService.archive_days_for_channel now treats values above
30 as hours (no provider offers a >30-day archive, while hour-based providers
send >= 24). Every consumer (EPG ingest, channel listing, EPG/catchup API
endpoints, scheduler) goes through this helper.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import EPGService


class ArchiveDurationHoursHeuristicTests(unittest.TestCase):
    """archive_days_for_channel converts hour-based values to days."""

    def test_168_hours_becomes_7_days(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 168}), 7)

    def test_240_hours_becomes_10_days(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 240}), 10)

    def test_720_hours_becomes_30_days(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 720}), 30)

    def test_numeric_string_hours_converted(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": "168"}), 7)

    def test_day_values_up_to_30_unchanged(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 7}), 7)
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 14}), 14)
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 30}), 30)

    def test_hours_just_above_threshold_never_round_to_zero(self):
        # 31 is treated as hours; integer division would give 0 days without a floor.
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 31}), 1)


class GetChannelArchiveDaysHoursTests(unittest.IsolatedAsyncioTestCase):
    """The provider-list lookup used by the API endpoints and scheduler applies the heuristic."""

    async def test_hours_provider_returns_days(self):
        service = EPGService()

        async def fake_get_live_streams():
            return [{"stream_id": 1, "name": "Sports HD", "tv_archive": 1, "tv_archive_duration": 168}]

        mock_client = MagicMock()
        mock_client.get_live_streams = AsyncMock(side_effect=fake_get_live_streams)
        mock_client.close = AsyncMock()

        with patch.object(service, "_get_client", return_value=mock_client):
            days = await service.get_channel_archive_days(MagicMock(), account_id=1, channel_id="1")

        self.assertEqual(days, 7)


if __name__ == "__main__":
    unittest.main()
