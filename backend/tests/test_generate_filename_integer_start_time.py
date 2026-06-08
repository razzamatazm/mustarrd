"""
Regression: generate_filename raises TypeError when start_time is an integer
and valid start_timestamp/stop_timestamp are also present in the program dict.

Root cause: _parse_program_times takes the timestamp branch when ts_start and
ts_stop are non-zero, so the isinstance(str) guard added by PR #198 is never
reached.  generate_filename then calls datetime.fromisoformat(int), which raises
TypeError — not ValueError — so the caller's `except ValueError` does not catch
it, producing HTTP 500.

These tests document the expected behaviour (no TypeError; graceful fallback to
now-based date) and currently FAIL.  They should pass once the fix lands.
"""

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import file_namer
from services.download_builder import _parse_program_times


_CHANNEL = {"name": "Test Channel", "stream_id": "123", "category_name": "General"}

# A program that has valid integer timestamps AND integer start_time/end_time.
# _parse_program_times succeeds (timestamp branch).
# generate_filename then receives the integer start_time.
_PROGRAM_WITH_INTEGER_TIMES = {
    "start_timestamp": 1685620800,
    "stop_timestamp": 1685624400,
    "start_time": 1685620800,   # integer — not a string
    "end_time": 1685624400,     # integer — not a string
    "title": "Test Show",
    "description": "",
}


class TestGenerateFilenameIntegerStartTime(unittest.TestCase):
    """generate_filename must not raise TypeError when start_time is an int."""

    def test_parse_program_times_succeeds_with_integer_start_time(self):
        """_parse_program_times takes the timestamp branch; should not raise."""
        # Verify the precondition: _parse_program_times is fine with this input.
        start_utc, end_utc, duration, _, _ = _parse_program_times(_PROGRAM_WITH_INTEGER_TIMES)
        self.assertIsNotNone(start_utc)
        self.assertGreater(duration, 0)

    def test_generate_filename_integer_start_time_does_not_raise_type_error(self):
        """
        generate_filename must not propagate TypeError for integer start_time.
        It should either fall back gracefully to datetime.utcnow() for the date
        or raise ValueError — but never TypeError.
        """
        try:
            filename = file_namer.generate_filename(
                _PROGRAM_WITH_INTEGER_TIMES, _CHANNEL, "default", None
            )
            # If it returns without error, verify it's a non-empty string.
            self.assertIsInstance(filename, str)
            self.assertTrue(filename.endswith(".ts"))
        except TypeError:
            self.fail(
                "generate_filename raised TypeError for integer start_time when "
                "valid timestamps are present. Expected graceful fallback or "
                "ValueError, not TypeError."
            )

    def test_generate_filename_integer_start_time_tv_show_does_not_raise_type_error(self):
        """Same assertion for the tv_show code path."""
        program = dict(_PROGRAM_WITH_INTEGER_TIMES)
        program["title"] = "The Wire S01E01 - The Target"
        try:
            filename = file_namer.generate_filename(program, _CHANNEL, "tv_show", None)
            self.assertTrue(filename.endswith(".ts"))
        except TypeError:
            self.fail(
                "generate_filename(tv_show) raised TypeError for integer start_time."
            )

    def test_generate_filename_integer_start_time_movie_does_not_raise_type_error(self):
        """Same assertion for the movie code path."""
        program = dict(_PROGRAM_WITH_INTEGER_TIMES)
        program["title"] = "Inception (2010)"
        try:
            filename = file_namer.generate_filename(program, _CHANNEL, "movie", None)
            self.assertTrue(filename.endswith(".ts"))
        except TypeError:
            self.fail(
                "generate_filename(movie) raised TypeError for integer start_time."
            )


if __name__ == "__main__":
    unittest.main()
