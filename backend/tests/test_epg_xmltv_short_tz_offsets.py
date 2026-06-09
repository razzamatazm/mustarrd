"""
Regression tests for short single-digit-hour TZ offsets in XMLTV timestamps.

Bug (MEDIUM): _parse_xmltv_time handles +05:30 (len=6) by stripping the colon,
but +5:30 (len=5) and +530 (len=4) bypass that check. Python's strptime("%z")
rejects both forms, falling to the except clause which silently stores UTC.

Impact: providers in India (+5:30), Iran (+3:30), Myanmar (+6:30),
Newfoundland (-3:30) etc. get all programs stored 30min-6.5 hours wrong.

After fix: +5:30 and +530 are zero-padded to +05:30 / +0530 before strptime.
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager

UTC_PLUS_5_30 = timezone(timedelta(hours=5, minutes=30))
UTC_MINUS_3_30 = timezone(timedelta(hours=-3, minutes=-30))


class ShortTZOffsetTests(unittest.TestCase):

    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def _parse(self, ts: str):
        return self.mgr._parse_xmltv_time(ts)

    def test_plus_5_colon_30_stored_correctly(self):
        """
        +5:30 (India/Sri Lanka/Nepal) must not be stored as UTC.

        Before fix: strptime("+5:30", "%z") raises ValueError -> UTC (offset 0).
        After fix: padded to +05:30 -> +0530 -> UTC+5:30.
        """
        dt = self._parse("20240115200000 +5:30")
        self.assertIsNotNone(dt, "Parse must not return None for +5:30 offset.")
        expected_utc = datetime(2024, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
            expected_utc,
            "20:00 +5:30 must convert to 14:30 UTC, not 20:00 UTC.",
        )

    def test_plus_530_no_colon_stored_correctly(self):
        """
        +530 (no colon) must be treated as UTC+5:30.

        Before fix: strptime("+530", "%z") raises ValueError -> UTC.
        After fix: padded to +0530 -> UTC+5:30.
        """
        dt = self._parse("20240115200000 +530")
        self.assertIsNotNone(dt, "Parse must not return None for +530 offset.")
        expected_utc = datetime(2024, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
            expected_utc,
            "20:00 +530 must convert to 14:30 UTC.",
        )

    def test_minus_3_colon_30_stored_correctly(self):
        """
        -3:30 (Newfoundland) must not be stored as UTC.

        Before fix: strptime("-3:30", "%z") raises ValueError -> UTC.
        After fix: padded to -03:30 -> -0330 -> UTC-3:30.
        """
        dt = self._parse("20240115120000 -3:30")
        self.assertIsNotNone(dt, "Parse must not return None for -3:30 offset.")
        expected_utc = datetime(2024, 1, 15, 15, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
            expected_utc,
            "12:00 -3:30 must convert to 15:30 UTC.",
        )

    def test_standard_plus_05_30_unchanged(self):
        """
        +05:30 (standard form) must still work after the fix.
        Regression guard: normalization must not break the existing path.
        """
        dt = self._parse("20240115200000 +05:30")
        self.assertIsNotNone(dt)
        expected_utc = datetime(2024, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
            expected_utc,
        )

    def test_standard_plus_0530_no_colon_unchanged(self):
        """
        +0530 (standard no-colon form) must still work.
        """
        dt = self._parse("20240115200000 +0530")
        self.assertIsNotNone(dt)
        expected_utc = datetime(2024, 1, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc),
            expected_utc,
        )


if __name__ == "__main__":
    unittest.main()
