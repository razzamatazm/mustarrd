"""
Regression tests for the EPG wipe caused by missing tv_archive_duration.

When a provider sets tv_archive=1 but omits tv_archive_duration, archive_days
returns 0. Previously the ingest deleted all EPGProgram rows for those channels
and skipped inserting new XMLTV programs. Both bugs are now fixed.
"""
import sys
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager
from services.epg_service import EPGService


class ArchiveDaysForChannelTests(unittest.TestCase):
    """EPGService.archive_days_for_channel covers common provider edge cases."""

    def test_missing_duration_returns_zero(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive": 1}), 0)

    def test_none_duration_returns_zero(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive": 1, "tv_archive_duration": None}), 0)

    def test_non_numeric_duration_returns_zero(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": "unlimited"}), 0)

    def test_numeric_string_duration_parsed(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": "7"}), 7)

    def test_int_duration_parsed(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 30}), 30)

    def test_negative_duration_clamped_to_zero(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": -5}), 0)

    def test_over_365_clamped(self):
        self.assertEqual(EPGService.archive_days_for_channel({"tv_archive_duration": 999}), 365)


class IterProgramsArchiveUnknownTests(unittest.TestCase):
    """_iter_programs must yield XMLTV programs for channels with archive_days==0."""

    _XMLTV = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <tv>
          <channel id="ch1">
            <display-name>Test Channel</display-name>
          </channel>
          <programme start="20260601120000 +0000" stop="20260601130000 +0000" channel="ch1">
            <title>Recent Program</title>
          </programme>
          <programme start="20260101120000 +0000" stop="20260101130000 +0000" channel="ch1">
            <title>Old Program</title>
          </programme>
        </tv>
    """).encode()

    def _make_channel_maps(self, archive_days):
        return {
            "stream_by_xmltv_id": {"ch1": "101"},
            "stream_by_name": {},
            "stream_info": {
                "101": {"name": "Test Channel", "has_archive": True, "archive_days": archive_days},
            },
        }

    def test_archive_days_zero_yields_all_xmltv_programs(self):
        """When archive_days==0 (duration missing from provider), all XMLTV programs are inserted."""
        manager = EPGIngestManager()
        now = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
        channel_maps = self._make_channel_maps(archive_days=0)

        programs = list(manager._iter_programs(self._XMLTV, channel_maps, now))

        titles = {p["title"] for p in programs}
        self.assertIn("Recent Program", titles)
        self.assertIn("Old Program", titles)

    def test_archive_days_positive_filters_old_programs(self):
        """When archive_days is set, programs before the cutoff are excluded (regression guard)."""
        manager = EPGIngestManager()
        now = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
        channel_maps = self._make_channel_maps(archive_days=7)

        programs = list(manager._iter_programs(self._XMLTV, channel_maps, now))

        titles = {p["title"] for p in programs}
        self.assertIn("Recent Program", titles)
        self.assertNotIn("Old Program", titles)

    def test_archive_days_zero_program_fields_populated(self):
        """Programs yielded for archive_days==0 channels have the expected fields."""
        manager = EPGIngestManager()
        now = datetime(2026, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
        channel_maps = self._make_channel_maps(archive_days=0)

        programs = list(manager._iter_programs(self._XMLTV, channel_maps, now))
        recent = next(p for p in programs if p["title"] == "Recent Program")

        self.assertEqual(recent["channel_id"], "101")
        self.assertEqual(recent["channel_name"], "Test Channel")
        self.assertIn("epg_id", recent)
        self.assertIn("start_time", recent)
        self.assertIn("end_time", recent)


if __name__ == "__main__":
    unittest.main()
