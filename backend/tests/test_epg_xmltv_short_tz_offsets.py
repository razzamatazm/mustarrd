"""
Regression tests: short numeric TZ offsets (+5:30, +530) silently stored as UTC.

_parse_xmltv_time colon-strips only when len(tz_part) == 6 and tz_part[3] == ':',
which handles "+05:30" -> "+0530" but misses single-digit-hour forms:

  "+5:30"  (len=5)  - strptime("%z") rejects single-digit hour -> ValueError -> UTC
  "+530"   (len=4)  - strptime("%z") rejects 3-digit value   -> ValueError -> UTC
  "-3:30"  (len=5)  - same issue

Affected time zones: India (+5:30), Iran (+3:30), Myanmar (+6:30),
Sri Lanka (+5:30), Afghanistan (+4:30), Newfoundland (-3:30).

Provider emits timestamps with non-padded hour; all programmes stored 3.5-6.5 h
off. Downloads fire at the wrong time; Browse EPG shows wrong slots.
"""
import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager

IST = timezone(timedelta(hours=5, minutes=30))
IRAN = timezone(timedelta(hours=3, minutes=30))
NFD = timezone(timedelta(hours=-3, minutes=-30))


class ShortTzOffsetTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def test_plus_5_colon_30_returns_ist_offset(self):
        """'+5:30' (len=5) must be interpreted as UTC+5:30, not UTC."""
        result = self.mgr._parse_xmltv_time("20240115200000 +5:30")
        self.assertIsNotNone(result, "'+5:30' offset returned None")
        self.assertEqual(
            result.utcoffset(),
            timedelta(hours=5, minutes=30),
            f"'+5:30' parsed as {result.utcoffset()}, expected +05:30 (UTC+5:30)",
        )

    def test_plus_530_no_colon_returns_ist_offset(self):
        """'+530' (len=4, no colon) must be interpreted as UTC+5:30, not UTC."""
        result = self.mgr._parse_xmltv_time("20240115200000 +530")
        self.assertIsNotNone(result, "'+530' offset returned None")
        self.assertEqual(
            result.utcoffset(),
            timedelta(hours=5, minutes=30),
            f"'+530' parsed as {result.utcoffset()}, expected +05:30 (UTC+5:30)",
        )

    def test_minus_3_colon_30_returns_nfd_offset(self):
        """'-3:30' (Newfoundland, len=5) must be interpreted as UTC-3:30, not UTC."""
        result = self.mgr._parse_xmltv_time("20240115200000 -3:30")
        self.assertIsNotNone(result, "'-3:30' offset returned None")
        self.assertEqual(
            result.utcoffset(),
            timedelta(hours=-3, minutes=-30),
            f"'-3:30' parsed as {result.utcoffset()}, expected -03:30",
        )

    def test_plus_3_colon_30_returns_iran_offset(self):
        """'+3:30' (Iran, len=5) must be interpreted as UTC+3:30, not UTC."""
        result = self.mgr._parse_xmltv_time("20240115200000 +3:30")
        self.assertIsNotNone(result, "'+3:30' offset returned None")
        self.assertEqual(
            result.utcoffset(),
            timedelta(hours=3, minutes=30),
            f"'+3:30' parsed as {result.utcoffset()}, expected +03:30",
        )

    def test_standard_padded_offset_still_works(self):
        """+05:30 (standard 6-char form) must continue to parse correctly."""
        result = self.mgr._parse_xmltv_time("20240115200000 +05:30")
        self.assertIsNotNone(result)
        self.assertEqual(result.utcoffset(), timedelta(hours=5, minutes=30))

    def test_standard_compact_offset_still_works(self):
        """+0530 (standard 5-char no-colon form) must continue to parse correctly."""
        result = self.mgr._parse_xmltv_time("20240115200000 +0530")
        self.assertIsNotNone(result)
        self.assertEqual(result.utcoffset(), timedelta(hours=5, minutes=30))


if __name__ == "__main__":
    unittest.main()
