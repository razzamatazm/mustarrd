"""
Regression tests for ISO-format XMLTV timestamps with named TZ abbreviations
silently returning None and dropping all programs.

Bug: _parse_xmltv_time has a named-TZ lookup (_NAMED_TZ_OFFSETS) only in the
compact-format code path (YYYYMMDDHHmmss + TZ). When the ISO-format path fires
(date part contains dashes or 'T'), the code constructs an iso_str such as
"2024-01-15T20:00:00EST" and calls datetime.fromisoformat(), which raises
ValueError on named TZ abbreviations. The except clause returns None, silently
dropping every programme whose timestamp uses an ISO date with a named TZ zone.

Common real-world triggers:
  - North American providers: "2024-01-15 20:00:00 EST"
  - European providers:       "2024-01-15 20:00:00 CET"
  - Australian providers:     "2024-01-15 20:00:00 ACST"

Expected: returns a timezone-aware datetime equivalent to the correct UTC time.
Actual:   returns None, program is silently skipped during EPG ingest.

Fix (maintainer): in the ISO path, when fromisoformat raises ValueError on a
3-part token where parts[2] is a letter-only string, check parts[2].upper()
against _NAMED_TZ_OFFSETS and construct the datetime manually.
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class ISOFormatNamedTZTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager.__new__(EPGIngestManager)

    def test_iso_space_separator_est_not_none(self):
        """'2024-01-15 20:00:00 EST' must not return None.

        Bug: ISO path builds '2024-01-15T20:00:00EST', fromisoformat raises
        ValueError, except returns None. All programs with EST in ISO format
        are silently dropped from the guide.
        """
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 EST")
        self.assertIsNotNone(
            result,
            "'2024-01-15 20:00:00 EST' returned None. ISO path does not apply "
            "_NAMED_TZ_OFFSETS; fromisoformat raises ValueError on 'EST' suffix "
            "and the program is silently dropped.",
        )

    def test_iso_space_separator_est_correct_utc(self):
        """'2024-01-15 20:00:00 EST' (UTC-5) must equal 01:00 UTC on 2024-01-16."""
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 EST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 1, 16, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected,
            f"EST ISO noon should be 2024-01-16 01:00 UTC, got {result_utc.isoformat()}",
        )

    def test_iso_space_separator_pst_not_none(self):
        """'2024-01-15 20:00:00 PST' must not return None."""
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 PST")
        self.assertIsNotNone(
            result,
            "'2024-01-15 20:00:00 PST' returned None. Same ISO+named-TZ gap as EST.",
        )

    def test_iso_space_separator_pst_correct_utc(self):
        """'2024-01-15 20:00:00 PST' (UTC-8) must equal 04:00 UTC on 2024-01-16."""
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 PST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 1, 16, 4, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)

    def test_iso_space_separator_cet_not_none(self):
        """'2024-06-01 12:00:00 CET' must not return None."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 CET")
        self.assertIsNotNone(
            result,
            "'2024-06-01 12:00:00 CET' returned None. European providers using ISO "
            "format with CET lose their entire guide silently.",
        )

    def test_iso_space_separator_cet_correct_utc(self):
        """'2024-06-01 12:00:00 CET' (UTC+1) must equal 11:00 UTC."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 CET")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)

    def test_iso_space_separator_acst_not_none(self):
        """'2024-06-01 12:00:00 ACST' must not return None."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 ACST")
        self.assertIsNotNone(
            result,
            "'2024-06-01 12:00:00 ACST' returned None. Australian Central providers "
            "using ISO format lose their entire guide.",
        )

    def test_iso_space_separator_acst_correct_utc(self):
        """'2024-06-01 12:00:00 ACST' (UTC+9:30) must equal 02:30 UTC."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 ACST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 6, 1, 2, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)

    def test_iso_t_separator_est_not_none(self):
        """'2024-01-15T20:00:00 EST' (T-separator with space before TZ) must not return None."""
        result = self.mgr._parse_xmltv_time("2024-01-15T20:00:00 EST")
        self.assertIsNotNone(
            result,
            "'2024-01-15T20:00:00 EST' returned None. T-separator ISO format with "
            "named TZ has the same gap: 2 parts where parts[1] is 'EST' not starting "
            "with +/-, so it is treated as a no-tz ISO string; EST is then silently "
            "ignored and the time is treated as UTC, shifting all programs by 5 hours.",
        )

    def test_iso_space_separator_msk_not_none(self):
        """'2024-06-01 12:00:00 MSK' must not return None."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 MSK")
        self.assertIsNotNone(
            result,
            "'2024-06-01 12:00:00 MSK' returned None. Russian providers using ISO "
            "format with MSK lose their entire guide.",
        )

    def test_iso_space_separator_msk_correct_utc(self):
        """'2024-06-01 12:00:00 MSK' (UTC+3) must equal 09:00 UTC."""
        result = self.mgr._parse_xmltv_time("2024-06-01 12:00:00 MSK")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)

    def test_iso_numeric_offset_still_works(self):
        """Regression: '2024-01-15 20:00:00 +0100' must still work after fix."""
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00 +0100")
        self.assertIsNotNone(result)
        self.assertEqual(result.utcoffset().total_seconds(), 3600)

    def test_iso_no_tz_still_works(self):
        """Regression: '2024-01-15 20:00:00' must still return UTC-stamped datetime."""
        result = self.mgr._parse_xmltv_time("2024-01-15 20:00:00")
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_compact_named_tz_still_works(self):
        """Regression: '20240115200000 EST' (compact format) must still work."""
        result = self.mgr._parse_xmltv_time("20240115200000 EST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected = datetime(2024, 1, 16, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected)


if __name__ == "__main__":
    unittest.main()
