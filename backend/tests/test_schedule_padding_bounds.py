import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.schedules import ScheduleCreate


class SchedulePaddingBoundsTests(unittest.TestCase):
    """
    BUG: pre_padding_minutes and post_padding_minutes are constrained only
    with conint(ge=0) — no upper bound. A value of 10000 is accepted silently.
    In build_download_from_program the padded_duration sanity check tests the
    raw program duration, not the padded value, so padded_duration=20060 minutes
    (~14 days) is passed to FFmpeg. A single malicious or mistaken request locks
    the download slot for days.

    These tests currently FAIL because no ValidationError is raised for large
    padding values. After the fix (conint(ge=0, le=120) or similar) they pass.
    """

    _VALID_PROGRAM = {
        "start_timestamp": 1000000000,
        "stop_timestamp": 1000003600,
        "title": "Test Show",
    }

    def _make(self, **kwargs):
        return ScheduleCreate(
            account_id=1,
            channel_id="101",
            channel_name="Test",
            program=self._VALID_PROGRAM,
            **kwargs,
        )

    def test_pre_padding_upper_bound_enforced(self):
        """
        pre_padding_minutes=10000 must raise ValidationError.

        Currently FAILS: ScheduleCreate accepts 10000 without error.
        Fix: add an upper bound (e.g. le=120) to conint on pre_padding_minutes.
        """
        with self.assertRaises(ValidationError):
            self._make(pre_padding_minutes=10000)

    def test_post_padding_upper_bound_enforced(self):
        """
        post_padding_minutes=10000 must raise ValidationError.

        Currently FAILS: ScheduleCreate accepts 10000 without error.
        Fix: add an upper bound (e.g. le=120) to conint on post_padding_minutes.
        """
        with self.assertRaises(ValidationError):
            self._make(post_padding_minutes=10000)

    def test_reasonable_padding_still_accepted(self):
        """Sanity: valid padding values within a sane bound must not raise."""
        schedule = self._make(pre_padding_minutes=30, post_padding_minutes=15)
        self.assertEqual(schedule.pre_padding_minutes, 30)
        self.assertEqual(schedule.post_padding_minutes, 15)

    def test_zero_padding_accepted(self):
        """Zero padding is the default and must remain valid."""
        schedule = self._make(pre_padding_minutes=0, post_padding_minutes=0)
        self.assertEqual(schedule.pre_padding_minutes, 0)
        self.assertEqual(schedule.post_padding_minutes, 0)


if __name__ == "__main__":
    unittest.main()
