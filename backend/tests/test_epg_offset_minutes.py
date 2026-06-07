"""
Regression tests for epg_offset_minutes being a dead letter.

AppSettings.epg_offset_minutes was stored to DB and cleared the EPG cache
on every PUT /settings but was never read by EPGService. Program times
displayed in the guide were unaffected by the setting.

The fix wires global_offset_minutes through _resolve_display_window so it
is applied in addition to the per-account guide_offset_hours.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import EPGService


def _utc(hour, minute=0):
    return datetime(2024, 1, 15, hour, minute, tzinfo=timezone.utc)


class ResolveDisplayWindowOffsetTests(unittest.TestCase):
    """_resolve_display_window applies global_offset_minutes on top of guide_offset_hours."""

    def setUp(self):
        self.svc = EPGService()

    def _account(self, guide_offset_hours=0):
        acc = MagicMock()
        acc.guide_offset_hours = guide_offset_hours
        return acc

    def test_zero_global_offset_no_shift(self):
        start, end = self.svc._resolve_display_window(
            _utc(20, 0), _utc(21, 0), self._account(), global_offset_minutes=0
        )
        self.assertEqual(start, _utc(20, 0))
        self.assertEqual(end, _utc(21, 0))

    def test_global_offset_60_minutes_shifts_forward(self):
        """Before the fix, global_offset_minutes was ignored; times were unchanged."""
        start, end = self.svc._resolve_display_window(
            _utc(20, 0), _utc(21, 0), self._account(), global_offset_minutes=60
        )
        self.assertEqual(start, _utc(21, 0), "Start must shift by +60 min.")
        self.assertEqual(end, _utc(22, 0), "End must shift by +60 min.")

    def test_negative_global_offset_shifts_backward(self):
        start, end = self.svc._resolve_display_window(
            _utc(20, 0), _utc(21, 0), self._account(), global_offset_minutes=-30
        )
        self.assertEqual(start, _utc(19, 30))
        self.assertEqual(end, _utc(20, 30))

    def test_global_offset_combines_with_account_offset(self):
        """Global offset adds to per-account guide_offset_hours."""
        # account has +1h, global adds +30m → net +90m
        start, end = self.svc._resolve_display_window(
            _utc(20, 0), _utc(21, 0),
            self._account(guide_offset_hours=1),
            global_offset_minutes=30,
        )
        self.assertEqual(start, _utc(21, 30))
        self.assertEqual(end, _utc(22, 30))

    def test_global_offset_none_account_no_crash(self):
        start, end = self.svc._resolve_display_window(
            _utc(20, 0), _utc(21, 0), None, global_offset_minutes=60
        )
        self.assertEqual(start, _utc(21, 0))
        self.assertEqual(end, _utc(22, 0))

    def test_serialize_program_applies_global_offset(self):
        """serialize_program must pass global_offset_minutes to _resolve_display_window."""
        row = MagicMock()
        row.start_time = _utc(20, 0)
        row.end_time = _utc(21, 0)
        row.id = 1
        row.epg_id = "ch:1000:2000"
        row.title = "Test Show"
        row.description = ""
        row.start_timestamp = 1000
        row.stop_timestamp = 2000
        row.provider_start = None
        row.provider_stop = None
        row.has_archive = True
        row.channel_id = "42"

        result = self.svc.serialize_program(row, self._account(), global_offset_minutes=120)

        expected_start = _utc(22, 0).isoformat()
        self.assertEqual(
            result["start_time"],
            expected_start,
            "serialize_program must shift start_time by global_offset_minutes.",
        )


if __name__ == "__main__":
    unittest.main()
