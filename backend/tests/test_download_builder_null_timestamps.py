import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_builder import _parse_program_times


class ParseProgramTimesNullTests(unittest.TestCase):
    def test_null_start_time_raises_value_error(self):
        """start_time=None with no timestamps raises ValueError, not TypeError (HTTP 400 not 500)."""
        program = {
            "start_timestamp": 0,
            "stop_timestamp": 0,
            "start_time": None,
            "end_time": None,
        }
        with self.assertRaises(ValueError) as ctx:
            _parse_program_times(program)
        self.assertIn("no valid start or end time", str(ctx.exception))

    def test_missing_start_time_raises_value_error(self):
        """start_time key absent with no timestamps raises ValueError."""
        program = {"start_timestamp": 0, "stop_timestamp": 0}
        with self.assertRaises(ValueError):
            _parse_program_times(program)

    def test_null_start_time_only_raises_value_error(self):
        """start_time=None but end_time present still raises ValueError."""
        program = {
            "start_timestamp": 0,
            "stop_timestamp": 0,
            "start_time": None,
            "end_time": "2026-03-30T18:00:00",
        }
        with self.assertRaises(ValueError):
            _parse_program_times(program)

    def test_valid_timestamps_still_work(self):
        """Regression: programs with valid UNIX timestamps continue to parse correctly."""
        import datetime
        program = {
            "start_timestamp": 1743354000,
            "stop_timestamp": 1743357600,
        }
        start, end, duration, _, _ = _parse_program_times(program)
        self.assertEqual(duration, 60)
        self.assertEqual(start.tzinfo, datetime.timezone.utc)

    def test_valid_iso_strings_still_work(self):
        """Regression: programs with valid ISO strings (no UNIX timestamps) continue to parse."""
        program = {
            "start_timestamp": 0,
            "stop_timestamp": 0,
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time": "2026-03-30T18:00:00+00:00",
        }
        _, _, duration, _, _ = _parse_program_times(program)
        self.assertEqual(duration, 60)

    def test_legacy_zero_timestamps_naive_datetime_treated_as_utc(self):
        """start_timestamp=0 (legacy row default) + naive DB datetime must be treated as UTC.

        Bug: naive datetimes from our DB are in UTC. When start_timestamp=0 caused
        the code to fall back to fromisoformat(), .timestamp() interpreted the result
        as local server time. On non-UTC servers this stored incorrect timestamps.
        """
        import datetime
        program = {
            "start_timestamp": 0,
            "stop_timestamp": 0,
            "start_time": "2026-03-30T17:00:00",
            "end_time": "2026-03-30T18:00:00",
        }
        start, end, duration, _, _ = _parse_program_times(program)
        self.assertEqual(start.tzinfo, datetime.timezone.utc)
        self.assertEqual(end.tzinfo, datetime.timezone.utc)
        self.assertEqual(start.hour, 17)
        self.assertEqual(end.hour, 18)
        self.assertEqual(duration, 60)

    def test_none_timestamps_naive_datetime_treated_as_utc(self):
        """start_timestamp=None with naive DB datetime must also be treated as UTC."""
        import datetime
        program = {
            "start_time": "2026-06-07T20:00:00",
            "end_time": "2026-06-07T22:00:00",
        }
        start, end, duration, _, _ = _parse_program_times(program)
        self.assertEqual(start.tzinfo, datetime.timezone.utc)
        self.assertEqual(end.tzinfo, datetime.timezone.utc)
        self.assertEqual(duration, 120)


if __name__ == "__main__":
    unittest.main()
