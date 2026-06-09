import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_builder import _parse_program_times
from api.schedules import _parse_program

# A timestamp value so large that datetime.fromtimestamp raises OverflowError
# on all supported platforms (Linux/macOS/Windows 64-bit).
_HUGE_TS = 99999999999999999999


class TestTimestampOverflowDownloadBuilder(unittest.TestCase):
    """_parse_program_times must raise ValueError (not OverflowError/OSError) for out-of-range timestamps."""

    def test_overflow_start_raises_value_error(self):
        program = {"start_timestamp": _HUGE_TS, "stop_timestamp": _HUGE_TS}
        with self.assertRaises(ValueError):
            _parse_program_times(program)

    def test_overflow_stop_raises_value_error(self):
        program = {"start_timestamp": 1743354000, "stop_timestamp": _HUGE_TS}
        with self.assertRaises(ValueError):
            _parse_program_times(program)

    def test_normal_timestamp_unaffected(self):
        """Regression: valid timestamps still parse correctly after the fix."""
        program = {"start_timestamp": 1743354000, "stop_timestamp": 1743357600}
        start, end, duration, _, _ = _parse_program_times(program)
        self.assertEqual(duration, 60)


class TestTimestampOverflowSchedules(unittest.TestCase):
    """_parse_program must raise ValueError (not OverflowError/OSError) for out-of-range timestamps."""

    def test_overflow_start_raises_value_error(self):
        program = {"start_timestamp": _HUGE_TS, "stop_timestamp": _HUGE_TS}
        with self.assertRaises(ValueError):
            _parse_program(program)

    def test_overflow_stop_raises_value_error(self):
        program = {"start_timestamp": 1743354000, "stop_timestamp": _HUGE_TS}
        with self.assertRaises(ValueError):
            _parse_program(program)

    def test_normal_timestamp_unaffected(self):
        """Regression: valid timestamps still parse correctly after the fix."""
        program = {"start_timestamp": 1743354000, "stop_timestamp": 1743357600}
        _, _, start_ts, _, duration, _, _ = _parse_program(program)
        self.assertEqual(start_ts, 1743354000)
        self.assertEqual(duration, 60)


if __name__ == "__main__":
    unittest.main()
