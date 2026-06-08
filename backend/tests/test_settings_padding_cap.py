"""Regression tests for settings default padding vs schedule cap mismatch.

PUT /api/settings accepts any value >= 0 for default_pre_padding_minutes and
default_post_padding_minutes. POST /api/schedules caps padding at 120 via
conint(ge=0, le=120). A default > 120 is stored, the schedule modal pre-fills
it, and every schedule creation fails with a 422 validation error that does not
name the settings page as the root cause.

Expected: SettingsUpdate should reject values above 120 to match the schedule cap.
Actual: SettingsUpdate accepts 121 and above.
"""
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.settings import SettingsUpdate


class DefaultPaddingUpperBoundTests(unittest.TestCase):
    """default_pre/post_padding_minutes must be <= 120 to match the schedule endpoint cap.

    POST /api/schedules uses conint(ge=0, le=120) for padding. If the settings
    default exceeds 120, the schedule modal pre-fills the invalid value, and every
    subsequent schedule creation fails with a Pydantic 422 that does not tell the
    operator to fix their settings.
    """

    def test_pre_padding_at_cap_accepted(self):
        m = SettingsUpdate(default_pre_padding_minutes=120)
        self.assertEqual(m.default_pre_padding_minutes, 120)

    def test_post_padding_at_cap_accepted(self):
        m = SettingsUpdate(default_post_padding_minutes=120)
        self.assertEqual(m.default_post_padding_minutes, 120)

    def test_pre_padding_above_cap_rejected(self):
        """default_pre_padding_minutes=121 must be rejected.

        Currently accepted (SettingsUpdate has no le constraint) but should fail
        because the schedule endpoint cap is 120. Storing 121 breaks all schedule
        creation until the operator manually resets the value.
        """
        with self.assertRaises(ValidationError):
            SettingsUpdate(default_pre_padding_minutes=121)

    def test_post_padding_above_cap_rejected(self):
        """default_post_padding_minutes=121 must be rejected.

        Same root cause as pre_padding: the schedule endpoint rejects values > 120,
        so storing 121 here silently disables schedule creation.
        """
        with self.assertRaises(ValidationError):
            SettingsUpdate(default_post_padding_minutes=121)

    def test_pre_padding_large_value_rejected(self):
        """Arbitrary large values (e.g., 9999) must be rejected."""
        with self.assertRaises(ValidationError):
            SettingsUpdate(default_pre_padding_minutes=9999)

    def test_post_padding_large_value_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(default_post_padding_minutes=9999)


if __name__ == "__main__":
    unittest.main()
