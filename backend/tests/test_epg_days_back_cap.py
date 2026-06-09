"""
Regression tests: Browse EPG must not hard-cap days_back at 14.

The /channels/{id}/epg and /channels/{id}/catchup endpoints declared
days_back with Query(..., le=14), silently truncating channels whose
provider archive window is longer (e.g. 30 days). The validation cap is
now 365; the real limit is still the channel's own archive duration via
actual_days = min(days_back, channel_archive_days) inside each handler.
"""
import inspect
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.channels import get_channel_epg, get_catchup_programs


def _days_back_bounds(handler):
    sig = inspect.signature(handler)
    default = sig.parameters["days_back"].default
    ge = le = None
    for constraint in getattr(default, "metadata", []):
        if getattr(constraint, "ge", None) is not None:
            ge = constraint.ge
        if getattr(constraint, "le", None) is not None:
            le = constraint.le
    return ge, le


class DaysBackCapTests(unittest.TestCase):

    def test_channel_epg_days_back_allows_long_archives(self):
        ge, le = _days_back_bounds(get_channel_epg)
        self.assertEqual(ge, 1)
        self.assertIsNotNone(le)
        self.assertGreaterEqual(le, 30, "days_back cap truncates archives longer than 14 days")

    def test_catchup_days_back_allows_long_archives(self):
        ge, le = _days_back_bounds(get_catchup_programs)
        self.assertEqual(ge, 1)
        self.assertIsNotNone(le)
        self.assertGreaterEqual(le, 30, "days_back cap truncates archives longer than 14 days")


if __name__ == "__main__":
    unittest.main()
