"""
Regression tests for missing timezone abbreviations in _NAMED_TZ_OFFSETS.

Bug (MEDIUM-HIGH): _NAMED_TZ_OFFSETS only covers 21 Western-centric timezone
abbreviations. Common abbreviations used by Australian (ACST/ACDT), Newfoundland
(NST/NDT), Russian (MSK), Singaporean (SGT), Korean (KST), and Atlantic Canadian
(AST/ADT) providers are absent. When _parse_xmltv_time encounters an unknown named
timezone, the datetime.strptime(tz_part, "%z") call raises ValueError, and the
except clause at line 677 silently returns dt.replace(tzinfo=timezone.utc),
storing the program offset by hours.

A provider serving "20260601120000 ACST" stores the program as 12:00 UTC instead
of 02:30 UTC, a 9.5-hour error. Every scheduled recording for that channel fires
against the wrong content segment.

Note: the previous named-timezone fix (PR that addressed EST/PST/CET/BST etc.)
only added abbreviations already known to the developer. The half-hour offset zones
(ACST +9:30, NST -3:30, NDT -2:30, ACDT +10:30) and several full-hour zones
(MSK, SGT, KST, AST, ADT) were not included.

Root cause: epg_ingest_manager.py line 677
  except ValueError:
      return dt.replace(tzinfo=timezone.utc)

Fix required: add ACST, ACDT, NST, NDT, MSK, SGT, KST, AST, ADT (and others)
to _NAMED_TZ_OFFSETS.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class MissingTimezoneAbbreviationTests(unittest.TestCase):
    """Each test asserts the correct UTC equivalent; all fail before the fix
    because the unknown abbreviation silently becomes UTC."""

    def setUp(self):
        self.manager = EPGIngestManager.__new__(EPGIngestManager)

    # ------------------------------------------------------------------
    # Australia Central (half-hour offsets)
    # ------------------------------------------------------------------

    def test_acst_returns_utc_minus_9h30m(self):
        """'20260601120000 ACST' (UTC+9:30) must be stored as 02:30 UTC.

        Bug: ACST not in _NAMED_TZ_OFFSETS. strptime raises ValueError.
        Fallback stores 12:00 UTC instead of 02:30 UTC: 9.5-hour error.
        Australian Central providers (SA, NT) have every program shifted.
        """
        result = self.manager._parse_xmltv_time("20260601120000 ACST")
        self.assertIsNotNone(result, "ACST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 2, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"ACST noon should be 02:30 UTC, got {result_utc.isoformat()}. "
            "ACST (UTC+9:30) is missing from _NAMED_TZ_OFFSETS and silently "
            "coerced to UTC, producing a 9.5-hour shift.",
        )

    def test_acdt_returns_utc_minus_10h30m(self):
        """'20260101120000 ACDT' (UTC+10:30) must be stored as 01:30 UTC.

        Bug: ACDT not in _NAMED_TZ_OFFSETS. South Australia and NT summer time
        shifts all programs 10.5 hours from where they should be.
        """
        result = self.manager._parse_xmltv_time("20260101120000 ACDT")
        self.assertIsNotNone(result, "ACDT timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 1, 1, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"ACDT noon should be 01:30 UTC, got {result_utc.isoformat()}. "
            "ACDT (UTC+10:30) is missing from _NAMED_TZ_OFFSETS.",
        )

    # ------------------------------------------------------------------
    # Newfoundland (half-hour offsets)
    # ------------------------------------------------------------------

    def test_nst_returns_utc_plus_3h30m(self):
        """'20260601120000 NST' (UTC-3:30) must be stored as 15:30 UTC.

        Bug: NST not in _NAMED_TZ_OFFSETS. Prior fixes (PRs #269, #274)
        corrected Newfoundland's numeric offset (+5:30 style) but did not
        add 'NST' as a named abbreviation. Providers using 'NST' in XMLTV
        still silently get UTC: 3.5-hour error, wrong content recorded.
        """
        result = self.manager._parse_xmltv_time("20260601120000 NST")
        self.assertIsNotNone(result, "NST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 15, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"NST noon should be 15:30 UTC, got {result_utc.isoformat()}. "
            "NST (UTC-3:30) is missing from _NAMED_TZ_OFFSETS. "
            "PRs #269/#274 fixed numeric +5:30 style but not the named form.",
        )

    def test_ndt_returns_utc_plus_2h30m(self):
        """'20260601120000 NDT' (UTC-2:30) must be stored as 14:30 UTC.

        Bug: NDT not in _NAMED_TZ_OFFSETS. Newfoundland Daylight Time.
        """
        result = self.manager._parse_xmltv_time("20260601120000 NDT")
        self.assertIsNotNone(result, "NDT timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"NDT noon should be 14:30 UTC, got {result_utc.isoformat()}. "
            "NDT (UTC-2:30) is missing from _NAMED_TZ_OFFSETS.",
        )

    # ------------------------------------------------------------------
    # Full-hour offsets
    # ------------------------------------------------------------------

    def test_msk_returns_utc_minus_3h(self):
        """'20260601120000 MSK' (UTC+3) must be stored as 09:00 UTC.

        Bug: MSK not in _NAMED_TZ_OFFSETS. Russian providers using MSK in
        XMLTV have all programs shifted 3 hours.
        """
        result = self.manager._parse_xmltv_time("20260601120000 MSK")
        self.assertIsNotNone(result, "MSK timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"MSK noon should be 09:00 UTC, got {result_utc.isoformat()}. "
            "MSK (UTC+3) is missing from _NAMED_TZ_OFFSETS.",
        )

    def test_sgt_returns_utc_minus_8h(self):
        """'20260601120000 SGT' (UTC+8) must be stored as 04:00 UTC.

        Bug: SGT not in _NAMED_TZ_OFFSETS. Singapore providers have all
        programs shifted 8 hours.
        """
        result = self.manager._parse_xmltv_time("20260601120000 SGT")
        self.assertIsNotNone(result, "SGT timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 4, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"SGT noon should be 04:00 UTC, got {result_utc.isoformat()}. "
            "SGT (UTC+8) is missing from _NAMED_TZ_OFFSETS.",
        )

    def test_kst_returns_utc_minus_9h(self):
        """'20260601120000 KST' (UTC+9) must be stored as 03:00 UTC.

        Bug: KST not in _NAMED_TZ_OFFSETS. Korean providers have all
        programs shifted 9 hours.
        """
        result = self.manager._parse_xmltv_time("20260601120000 KST")
        self.assertIsNotNone(result, "KST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 3, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"KST noon should be 03:00 UTC, got {result_utc.isoformat()}. "
            "KST (UTC+9) is missing from _NAMED_TZ_OFFSETS.",
        )

    def test_ast_returns_utc_plus_4h(self):
        """'20260601120000 AST' (UTC-4) must be stored as 16:00 UTC.

        Bug: AST not in _NAMED_TZ_OFFSETS. Atlantic Standard Time is used by
        Atlantic Canadian providers (New Brunswick, Nova Scotia, PEI).
        Programs shift 4 hours from intended time.
        """
        result = self.manager._parse_xmltv_time("20260601120000 AST")
        self.assertIsNotNone(result, "AST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 16, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"AST noon should be 16:00 UTC, got {result_utc.isoformat()}. "
            "AST (UTC-4) is missing from _NAMED_TZ_OFFSETS.",
        )

    def test_adt_returns_utc_plus_3h(self):
        """'20260601120000 ADT' (UTC-3) must be stored as 15:00 UTC.

        Bug: ADT not in _NAMED_TZ_OFFSETS. Atlantic Daylight Time (Atlantic
        Canada summer).
        """
        result = self.manager._parse_xmltv_time("20260601120000 ADT")
        self.assertIsNotNone(result, "ADT timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 15, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"ADT noon should be 15:00 UTC, got {result_utc.isoformat()}. "
            "ADT (UTC-3) is missing from _NAMED_TZ_OFFSETS.",
        )

    # ------------------------------------------------------------------
    # Regression guard: existing abbreviations must still work
    # ------------------------------------------------------------------

    def test_ist_still_correct(self):
        """IST (UTC+5:30) must still return the correct value after the fix."""
        result = self.manager._parse_xmltv_time("20260601120000 IST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 6, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected_utc)

    def test_jst_still_correct(self):
        """JST (UTC+9) must still return the correct value."""
        result = self.manager._parse_xmltv_time("20260601120000 JST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2026, 6, 1, 3, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected_utc)


if __name__ == "__main__":
    unittest.main()
