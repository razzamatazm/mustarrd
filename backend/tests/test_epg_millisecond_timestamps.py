"""
Regression tests for millisecond timestamps crashing EPG fetch with ValueError.

Some providers return 13-digit millisecond Unix timestamps in start_timestamp /
stop_timestamp fields. datetime.fromtimestamp(1774864800000) raises
ValueError: year 58219 is out of range. EPGService._coerce_timestamp now
divides by 1000 when the value exceeds 10_000_000_000.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import EPGService


class CoerceTimestampTests(unittest.TestCase):
    """EPGService._coerce_timestamp normalizes ms timestamps to seconds."""

    def setUp(self):
        self.svc = EPGService()

    def test_second_timestamp_unchanged(self):
        self.assertEqual(self.svc._coerce_timestamp(1700000000), 1700000000)

    def test_millisecond_timestamp_divided(self):
        self.assertEqual(self.svc._coerce_timestamp(1774864800000), 1774864800)

    def test_millisecond_string_divided(self):
        self.assertEqual(self.svc._coerce_timestamp("1774864800000"), 1774864800)

    def test_none_returns_zero(self):
        self.assertEqual(self.svc._coerce_timestamp(None), 0)

    def test_invalid_returns_zero(self):
        self.assertEqual(self.svc._coerce_timestamp("bad"), 0)

    def test_boundary_value_10_billion_not_divided(self):
        self.assertEqual(self.svc._coerce_timestamp(10_000_000_000), 10_000_000_000)

    def test_boundary_value_above_10_billion_divided(self):
        self.assertEqual(self.svc._coerce_timestamp(10_000_000_001), 10_000_000)


class ProcessEpgEntryMillisecondTest(unittest.TestCase):
    """_process_epg_entry no longer raises ValueError on ms timestamps."""

    def setUp(self):
        self.svc = EPGService()

    def test_millisecond_entry_does_not_raise(self):
        import base64
        entry = {
            "start_timestamp": 1774864800000,
            "stop_timestamp": 1774868400000,
            "title": base64.b64encode(b"Test Show").decode(),
            "description": "",
            "has_archive": 1,
            "channel_id": "123",
        }
        try:
            self.svc._process_epg_entry(entry, account=None, fallback_channel_id="123")
        except ValueError as exc:
            self.fail(f"_process_epg_entry raised ValueError: {exc}")


if __name__ == "__main__":
    unittest.main()
