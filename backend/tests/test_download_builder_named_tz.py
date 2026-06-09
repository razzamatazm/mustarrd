import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_builder import _normalize_provider_start_token


class NormalizeProviderStartTokenNamedTZTests(unittest.TestCase):
    """_normalize_provider_start_token strips named TZ abbreviations from provider_start.

    Some providers include named TZ abbreviations in XMLTV start attributes
    (e.g. "20240115203000 EST").  _parse_xmltv_time handles these and stores
    correct UTC timestamps, but _iter_programs also stores the raw XMLTV string
    as provider_start.  _normalize_provider_start_token previously failed to
    strip the letters-only suffix, causing all strptime attempts to fail and
    returning None.  build_timeshift_url then fell back to UTC wall-clock time,
    fetching content hours off from the intended show.
    """

    def test_est_stripped(self):
        result = _normalize_provider_start_token("20240115203000 EST", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_pst_stripped(self):
        result = _normalize_provider_start_token("20240115203000 PST", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_cet_stripped(self):
        result = _normalize_provider_start_token("20240115203000 CET", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_msk_stripped(self):
        result = _normalize_provider_start_token("20240115203000 MSK", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_pre_padding_applied_with_named_tz(self):
        result = _normalize_provider_start_token("20240115203000 EST", 5)
        self.assertEqual(result, "2024-01-15:20-25")

    def test_numeric_offset_still_works(self):
        """Named-TZ strip must not break existing numeric-offset handling."""
        result = _normalize_provider_start_token("20240115203000 +0200", 0)
        self.assertEqual(result, "2024-01-15:20-30")

    def test_no_tz_still_works(self):
        result = _normalize_provider_start_token("20240115203000", 0)
        self.assertEqual(result, "2024-01-15:20-30")


if __name__ == "__main__":
    unittest.main()
