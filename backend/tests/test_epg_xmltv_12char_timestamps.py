"""
Regression tests for 12-char XMLTV timestamps silently dropping all programs.

XMLTV allows omitting seconds: YYYYMMDDHHmm (12 digits). The prior code
gated on len >= 14 so bare 12-char values returned None, and
"202306011200 +0000" passed the gate but sliced value[:14] = "202306011200 +"
which failed strptime. Both cases caused _iter_programs to silently skip every
program from providers using this format.

_parse_xmltv_time now splits on the first space to isolate the date digits
before checking length, and accepts both 12-digit and 14-digit date parts.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class ParseXmltvTime12CharTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def test_12_char_bare_returns_utc(self):
        result = self.mgr._parse_xmltv_time("202306011200")
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2023, 6, 1, 12, 0, tzinfo=timezone.utc))

    def test_12_char_with_utc_offset_returns_utc(self):
        result = self.mgr._parse_xmltv_time("202306011200 +0000")
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2023, 6, 1, 12, 0, tzinfo=timezone.utc))

    def test_12_char_with_positive_offset_applied(self):
        result = self.mgr._parse_xmltv_time("202306011200 +0200")
        self.assertIsNotNone(result)
        self.assertEqual(result.utcoffset().total_seconds(), 7200)
        self.assertEqual(result.hour, 12)

    def test_12_char_with_negative_offset_applied(self):
        result = self.mgr._parse_xmltv_time("202306011200 -0500")
        self.assertIsNotNone(result)
        self.assertEqual(result.utcoffset().total_seconds(), -18000)

    def test_14_char_bare_still_works(self):
        result = self.mgr._parse_xmltv_time("20230601120000")
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

    def test_14_char_with_offset_still_works(self):
        result = self.mgr._parse_xmltv_time("20230601120000 +0000")
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

    def test_14_char_with_z_suffix_still_works(self):
        result = self.mgr._parse_xmltv_time("20230601120000Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_none_input_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time(""))

    def test_too_short_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time("2023060112"))

    def test_garbage_returns_none(self):
        self.assertIsNone(self.mgr._parse_xmltv_time("notadate"))


if __name__ == "__main__":
    unittest.main()
