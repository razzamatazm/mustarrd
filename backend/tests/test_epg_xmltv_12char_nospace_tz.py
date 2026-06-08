"""
Regression test: 12-char XMLTV timestamps with no-space timezone offset silently
return None, dropping every programme from providers that use this format.

Root cause: _parse_xmltv_time checks `elif len(value) >= 14` before `elif len >= 12`.
For "202311152000+0200" (17 chars, 12-char date + inline TZ with no space), the
first branch fires and slices value[:14] = "202311152000+0" which includes the first
two chars of the TZ offset.  strptime("%Y%m%d%H%M%S") then raises ValueError on
"+0" in the seconds position, and _parse_xmltv_time returns None.

All programmes on channels that use "YYYYMMDDHHmm+HHMM" or "YYYYMMDDHHmm-HHMM"
(no space between date and TZ) are silently dropped during XMLTV ingest.

Compare: "20231115200000+0200" (14-char date, no space) works correctly -- the first
14 chars are all digits so the slice is valid.  Only the 12-char variant is broken.
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class ParseXmltvTime12CharNospaceTzTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def test_12_char_positive_tz_nospace_not_none(self):
        """'202311152000+0200' must not return None.

        Bug: len("202311152000+0200") == 17 >= 14, so value[:14] == "202311152000+0"
        which strptime("%Y%m%d%H%M%S") rejects (+0 is not valid seconds). Returns None.
        All programmes from providers using this format are silently dropped.
        """
        result = self.mgr._parse_xmltv_time("202311152000+0200")
        self.assertIsNotNone(
            result,
            "12-char timestamp '202311152000+0200' (no space before TZ) returned None. "
            "_parse_xmltv_time slices value[:14]='202311152000+0' when len>=14, "
            "then strptime('%Y%m%d%H%M%S') fails on the +0 seconds fragment.",
        )

    def test_12_char_positive_tz_nospace_correct_offset(self):
        """'202311152000+0200' must resolve to 18:00 UTC (20:00 local - 2 hours)."""
        result = self.mgr._parse_xmltv_time("202311152000+0200")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2023, 11, 15, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected,
            f"Expected 18:00 UTC for 20:00 +0200, got {result_utc.isoformat()}.",
        )

    def test_12_char_negative_tz_nospace_not_none(self):
        """'202311152000-0500' must not return None (EST no-space)."""
        result = self.mgr._parse_xmltv_time("202311152000-0500")
        self.assertIsNotNone(
            result,
            "12-char timestamp '202311152000-0500' (no space before -0500) returned None.",
        )

    def test_12_char_negative_tz_nospace_correct_offset(self):
        """'202311152000-0500' must resolve to 01:00 UTC next day (20:00 + 5 hours)."""
        result = self.mgr._parse_xmltv_time("202311152000-0500")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2023, 11, 16, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected,
            f"Expected 2023-11-16 01:00 UTC for 20:00 -0500, got {result_utc.isoformat()}.",
        )

    def test_12_char_utc_nospace_not_none(self):
        """'202311152000+0000' (UTC, no space) must not return None."""
        result = self.mgr._parse_xmltv_time("202311152000+0000")
        self.assertIsNotNone(
            result,
            "12-char timestamp '202311152000+0000' (no space, UTC offset) returned None.",
        )

    def test_12_char_utc_nospace_correct_time(self):
        """'202311152000+0000' must resolve to 20:00 UTC."""
        result = self.mgr._parse_xmltv_time("202311152000+0000")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2023, 11, 15, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected,
            f"Expected 20:00 UTC for 20:00 +0000, got {result_utc.isoformat()}.",
        )

    def test_14_char_positive_tz_nospace_still_works(self):
        """Regression guard: 14-char no-space TZ must still parse correctly."""
        result = self.mgr._parse_xmltv_time("20231115200000+0200")
        self.assertIsNotNone(result, "14-char no-space TZ '20231115200000+0200' returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2023, 11, 15, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)

    def test_12_char_space_tz_still_works(self):
        """Regression guard: 12-char space-separated TZ must still parse."""
        result = self.mgr._parse_xmltv_time("202311152000 +0200")
        self.assertIsNotNone(result, "12-char space-TZ '202311152000 +0200' returned None.")


if __name__ == "__main__":
    unittest.main()
