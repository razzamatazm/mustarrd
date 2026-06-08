import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.schedules import _parse_program
from services.download_builder import _parse_program_times


class TestIntegerStartTimeCrash(unittest.TestCase):
    """
    Regression: integer start_time/end_time in program dict crashes
    datetime.fromisoformat() with TypeError instead of returning HTTP 400.
    """

    def test_parse_program_integer_start_time_raises_value_error(self):
        program = {
            "start_time": 1700000000,
            "end_time": 1700003600,
        }
        with self.assertRaises(ValueError):
            _parse_program(program)

    def test_parse_program_integer_start_time_does_not_raise_type_error(self):
        program = {
            "start_time": 1700000000,
            "end_time": 1700003600,
        }
        try:
            _parse_program(program)
        except TypeError:
            self.fail("_parse_program raised TypeError for integer start_time")
        except ValueError:
            pass  # expected

    def test_parse_program_times_integer_start_time_raises_value_error(self):
        program = {
            "start_time": 1700000000,
            "end_time": 1700003600,
        }
        with self.assertRaises(ValueError):
            _parse_program_times(program)

    def test_parse_program_times_integer_start_time_does_not_raise_type_error(self):
        program = {
            "start_time": 1700000000,
            "end_time": 1700003600,
        }
        try:
            _parse_program_times(program)
        except TypeError:
            self.fail("_parse_program_times raised TypeError for integer start_time")
        except ValueError:
            pass  # expected

    def test_parse_program_string_times_still_work(self):
        import datetime as dt
        program = {
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time": "2026-03-30T18:00:00+00:00",
        }
        _, _, start_ts, stop_ts, duration, _, _ = _parse_program(program)
        expected_start = int(dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(start_ts, expected_start)
        self.assertEqual(duration, 60)

    def test_parse_program_times_string_times_still_work(self):
        import datetime as dt
        program = {
            "start_time": "2026-03-30T17:00:00+00:00",
            "end_time": "2026-03-30T18:00:00+00:00",
        }
        start_utc, _, duration, _, _ = _parse_program_times(program)
        expected = dt.datetime(2026, 3, 30, 17, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(start_utc, expected)
        self.assertEqual(duration, 60)


if __name__ == "__main__":
    unittest.main()
