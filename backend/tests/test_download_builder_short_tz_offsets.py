import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_builder import _normalize_provider_start_token


class NormalizeProviderStartTokenShortTZTests(unittest.TestCase):
    """_normalize_provider_start_token zero-pads single-digit-hour TZ offsets.

    Providers serving India (+5:30), Newfoundland (-3:30), or Iran (+3:30)
    emit offsets with a 1-digit hour.  Before this fix the strip regex required
    two hour digits; the offset stayed in 'bare', all strptime attempts failed,
    and the function returned None.  build_timeshift_url fell back to UTC,
    putting IST users 5.5 hours off from the intended content.
    """

    def test_plus_colon_form_ist(self):
        """'+5:30' (India IST) is stripped; wall-clock time returned."""
        result = _normalize_provider_start_token("20240115203000 +5:30", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_compact_form_ist(self):
        """'+530' compact form (India IST) is stripped; wall-clock time returned."""
        result = _normalize_provider_start_token("20240115203000 +530", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_negative_colon_form_nst(self):
        """-3:30' (Newfoundland NST) is stripped; wall-clock time returned."""
        result = _normalize_provider_start_token("20240115203000 -3:30", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_plus_colon_form_irst(self):
        """'+3:30' (Iran IRST) is stripped; wall-clock time returned."""
        result = _normalize_provider_start_token("20240115120000 +3:30", 0)
        self.assertEqual(result, "2024-01-15:12-00")

    def test_standard_two_digit_offset_unchanged(self):
        """+0530' (already zero-padded) continues to work."""
        result = _normalize_provider_start_token("20240115203000 +0530", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_standard_colon_offset_unchanged(self):
        """'+05:30' (padded colon form) continues to work."""
        result = _normalize_provider_start_token("20240115203000 +05:30", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_utc_offset_unchanged(self):
        """+0000' (UTC) continues to work."""
        result = _normalize_provider_start_token("20240115120000 +0000", 0)
        self.assertEqual(result, "2024-01-15:12-00")

    def test_pre_padding_applied_with_short_tz(self):
        """Pre-padding is subtracted after the wall-clock time is extracted."""
        result = _normalize_provider_start_token("20240115203000 +5:30", 30)
        self.assertEqual(result, "2024-01-15:20-00")

    def test_empty_token_returns_none(self):
        """Empty token returns None regardless."""
        self.assertIsNone(_normalize_provider_start_token("", 0))

    def test_none_token_returns_none(self):
        """None token returns None without raising."""
        self.assertIsNone(_normalize_provider_start_token(None, 0))


if __name__ == "__main__":
    unittest.main()
