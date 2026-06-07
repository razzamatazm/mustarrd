import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.settings import SettingsUpdate


class TranscodeFormatValidationTests(unittest.TestCase):
    """transcode_format must be one of ts/mp4/mkv; arbitrary strings are rejected."""

    def test_valid_formats_accepted(self):
        for fmt in ("ts", "mp4", "mkv"):
            m = SettingsUpdate(transcode_format=fmt)
            self.assertEqual(m.transcode_format, fmt)

    def test_invalid_format_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(transcode_format="avi")

    def test_none_accepted(self):
        m = SettingsUpdate(transcode_format=None)
        self.assertIsNone(m.transcode_format)


class HwAccelValidationTests(unittest.TestCase):
    """hw_accel must be one of cpu/videotoolbox/nvenc/amf/vaapi; arbitrary strings are rejected."""

    def test_valid_accels_accepted(self):
        for accel in ("cpu", "videotoolbox", "nvenc", "amf", "vaapi"):
            m = SettingsUpdate(hw_accel=accel)
            self.assertEqual(m.hw_accel, accel)

    def test_invalid_accel_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(hw_accel="cuda")

    def test_none_accepted(self):
        m = SettingsUpdate(hw_accel=None)
        self.assertIsNone(m.hw_accel)


class MinFreeSpaceValidationTests(unittest.TestCase):
    """min_free_space_gb must be >= 1; 0 and negative values are rejected."""

    def test_positive_value_accepted(self):
        m = SettingsUpdate(min_free_space_gb=10)
        self.assertEqual(m.min_free_space_gb, 10)

    def test_zero_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(min_free_space_gb=0)

    def test_negative_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(min_free_space_gb=-1)

    def test_none_accepted(self):
        m = SettingsUpdate(min_free_space_gb=None)
        self.assertIsNone(m.min_free_space_gb)


if __name__ == "__main__":
    unittest.main()
