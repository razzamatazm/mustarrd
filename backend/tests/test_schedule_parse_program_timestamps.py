import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.schedules import _parse_program


class TestScheduleParseProgramTimestamps(unittest.TestCase):
    """
    Regression: api/schedules.py:_parse_program has the same falsy timestamp
    guard that download_builder.py had before PR #88.

    PR #88 introduced _coerce_ts in download_builder.py, which converts string
    timestamps to int before the truthiness check. That means string "0" becomes
    int 0 (falsy) and correctly falls through to the start_time/end_time branch.

    schedules.py still uses the raw value: `if start_timestamp and stop_timestamp:`
    So a string "0" (truthy non-empty string) takes the timestamp branch, calls
    datetime.fromtimestamp(0), and returns epoch 1970 datetimes. The real
    start_time/end_time fields are silently ignored.

    Impact: a schedule created with start_timestamp="0" stores epoch 1970 as its
    program times. available_at_utc() returns 1970, which is past the catchup
    window, so the scheduler immediately marks the recording FAILED.
    """

    def test_string_zero_start_timestamp_ignores_string_times(self):
        """String '0' start_timestamp is truthy; schedules.py takes the timestamp
        branch and returns epoch 1970, ignoring the valid start_time/end_time strings.

        Fails today: start_ts == 0 (epoch 1970), not the 2026 time from start_time.
        After fix (apply _coerce_ts or equivalent): start_ts matches start_time.
        """
        import datetime as dt
        program = {
            "start_timestamp": "0",
            "stop_timestamp": "7200",
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time":   "2026-03-30T18:00:00+00:00",
        }
        _, _, start_ts, stop_ts, duration, _, _ = _parse_program(program)
        expected_start = int(
            dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        )
        expected_stop = int(
            dt.datetime(2026, 3, 30, 18, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        )
        self.assertNotEqual(
            start_ts, 0,
            "start_ts must not be epoch 0 (1970); string '0' should not be used as a timestamp",
        )
        self.assertEqual(start_ts, expected_start)
        self.assertEqual(stop_ts, expected_stop)
        self.assertEqual(duration, 60)

    # --- regression guards ---

    def test_valid_nonzero_integer_timestamps_still_work(self):
        """Regression: valid integer timestamps must continue to parse correctly."""
        program = {
            "start_timestamp": 1743354000,
            "stop_timestamp": 1743357600,
        }
        _, _, start_ts, stop_ts, duration, _, _ = _parse_program(program)
        self.assertEqual(start_ts, 1743354000)
        self.assertEqual(stop_ts, 1743357600)
        self.assertEqual(duration, 60)

    def test_valid_nonzero_string_timestamps_still_work(self):
        """Regression: non-zero string timestamps must parse correctly."""
        program = {
            "start_timestamp": "1743354000",
            "stop_timestamp": "1743357600",
        }
        _, _, start_ts, stop_ts, duration, _, _ = _parse_program(program)
        self.assertEqual(start_ts, 1743354000)
        self.assertEqual(stop_ts, 1743357600)
        self.assertEqual(duration, 60)

    def test_integer_zero_timestamps_with_iso_strings(self):
        """Integer 0 timestamps already fall to string branch; strings must be used."""
        import datetime as dt
        program = {
            "start_timestamp": 0,
            "stop_timestamp": 0,
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time":   "2026-03-30T18:00:00+00:00",
        }
        _, _, start_ts, stop_ts, duration, _, _ = _parse_program(program)
        expected_start = int(
            dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        )
        self.assertEqual(start_ts, expected_start)
        self.assertEqual(duration, 60)

    def test_no_times_raises_value_error(self):
        """No usable time data must raise ValueError, not crash."""
        program = {"start_timestamp": 0, "stop_timestamp": 0}
        with self.assertRaises(ValueError):
            _parse_program(program)

    def test_no_timestamps_iso_strings_work(self):
        """No timestamp fields, only ISO strings: must parse correctly."""
        program = {
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time":   "2026-03-30T18:00:00+00:00",
        }
        _, _, _, _, duration, _, _ = _parse_program(program)
        self.assertEqual(duration, 60)

    def test_empty_string_start_timestamp_falls_through_to_iso(self):
        """Empty string start_timestamp is non-numeric; must fall through to ISO strings."""
        import datetime as dt
        program = {
            "start_timestamp": "",
            "stop_timestamp": "",
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time":   "2026-03-30T18:00:00+00:00",
        }
        _, _, start_ts, _, duration, _, _ = _parse_program(program)
        expected = int(dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(start_ts, expected)
        self.assertEqual(duration, 60)

    def test_nonnumeric_start_timestamp_falls_through_to_iso(self):
        """Non-numeric start_timestamp must fall through to ISO strings, not crash."""
        import datetime as dt
        program = {
            "start_timestamp": "abc",
            "stop_timestamp": "xyz",
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time":   "2026-03-30T18:00:00+00:00",
        }
        _, _, start_ts, _, duration, _, _ = _parse_program(program)
        expected = int(dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(start_ts, expected)
        self.assertEqual(duration, 60)


if __name__ == "__main__":
    unittest.main()
