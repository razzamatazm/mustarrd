"""
Regression tests for ISO 8601 XMLTV timestamps silently dropping all programs.

Some XMLTV providers emit timestamps in ISO format (with dashes and colons)
rather than the compact XMLTV-spec format (YYYYMMDDHHmmss):

    start="2024-01-15 20:00:00 +0100"   <-- ISO with space separator
    start="2024-01-15T20:00:00+01:00"   <-- ISO 8601 with T separator

_parse_xmltv_time splits on the first space. For "2024-01-15 20:00:00 +0100"
the date_part becomes "2024-01-15" (10 chars). The function checks for 14 or
12 digit parts only, so len == 10 falls through to `return None`. Every
programme for the account is silently skipped. No warning is emitted. The
entire guide goes empty with no user-visible error.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class ParseXmltvTimeISOFormatTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def test_iso_date_time_space_separator_no_tz_returns_utc(self):
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00")
        self.assertIsNotNone(result, "ISO timestamp with space separator returned None")
        self.assertEqual(result, datetime(2024, 1, 15, 20, 0, 0, tzinfo=timezone.utc))

    def test_iso_date_time_space_separator_with_utc_offset(self):
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 +0100")
        self.assertIsNotNone(result, "ISO timestamp with +0100 offset returned None")
        self.assertEqual(result.hour, 20)
        self.assertEqual(result.utcoffset().total_seconds(), 3600)

    def test_iso_date_time_space_separator_with_negative_offset(self):
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 -0500")
        self.assertIsNotNone(result, "ISO timestamp with -0500 offset returned None")
        self.assertEqual(result.utcoffset().total_seconds(), -18000)

    def test_iso_date_only_time_no_seconds(self):
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00")
        self.assertIsNotNone(result, "ISO timestamp without seconds returned None")
        self.assertEqual(result, datetime(2024, 1, 15, 20, 0, tzinfo=timezone.utc))

    def test_iso_t_separator_no_tz(self):
        result = self.mgr._parse_xmltv_time("2024-01-15T20:00:00")
        self.assertIsNotNone(result, "ISO 8601 T-separator timestamp returned None")
        self.assertEqual(result, datetime(2024, 1, 15, 20, 0, 0, tzinfo=timezone.utc))

    def test_iso_t_separator_with_colon_offset(self):
        result = self.mgr._parse_xmltv_time("2024-01-15T20:00:00+01:00")
        self.assertIsNotNone(result, "ISO 8601 T-separator with colon offset returned None")
        self.assertEqual(result.hour, 20)
        self.assertEqual(result.utcoffset().total_seconds(), 3600)

    def test_iso_t_separator_with_z_suffix(self):
        result = self.mgr._parse_xmltv_time("2024-01-15T20:00:00Z")
        self.assertIsNotNone(result, "ISO 8601 Z-suffix timestamp returned None")
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_compact_format_still_works_alongside_iso(self):
        """Existing compact format must not regress when ISO support is added."""
        result = self.mgr._parse_xmltv_time("20240115200000 +0100")
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 20)

    def test_empty_string_still_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time(""))

    def test_garbage_still_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time("notadate"))


if __name__ == "__main__":
    unittest.main()
