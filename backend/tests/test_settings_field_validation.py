import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.settings import SettingsUpdate, get_settings
from models import AppSettings


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


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


class MinFreeSpaceNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """Stored min_free_space_gb < 1 must be coerced to 25 at GET time.

    Before fix: the normalization only caught None; a stored 0 (possible via
    the old API that had no lower-bound constraint) passed through unchanged and
    caused every subsequent settings save to 422 because SettingsUpdate now
    requires min_free_space_gb >= 1.
    After fix: the condition also catches values < 1 and resets them to 25.
    """

    async def test_zero_stored_normalizes_to_25(self):
        settings = AppSettings()
        settings.download_folder = "/downloads"
        settings.completed_folder = "/completed"
        settings.min_free_space_gb = 0

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_ScalarResult(settings))

        fake_config_dir = MagicMock()
        fake_config_dir.__truediv__ = lambda self, other: MagicMock(exists=lambda: False)

        with patch("api.settings.is_docker_env", return_value=False), \
             patch("api.settings.is_desktop_env", return_value=False), \
             patch("api.settings.ensure_config_files", return_value=fake_config_dir):
            result = await get_settings(None, session)

        self.assertEqual(settings.min_free_space_gb, 25)
        self.assertEqual(result["min_free_space_gb"], 25)

    async def test_valid_value_not_overwritten(self):
        settings = AppSettings()
        settings.download_folder = "/downloads"
        settings.completed_folder = "/completed"
        settings.min_free_space_gb = 10

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_ScalarResult(settings))

        fake_config_dir = MagicMock()
        fake_config_dir.__truediv__ = lambda self, other: MagicMock(exists=lambda: False)

        with patch("api.settings.is_docker_env", return_value=False), \
             patch("api.settings.is_desktop_env", return_value=False), \
             patch("api.settings.ensure_config_files", return_value=fake_config_dir):
            result = await get_settings(None, session)

        self.assertEqual(settings.min_free_space_gb, 10)
        self.assertEqual(result["min_free_space_gb"], 10)


if __name__ == "__main__":
    unittest.main()
