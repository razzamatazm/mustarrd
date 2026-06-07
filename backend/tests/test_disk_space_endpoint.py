"""
Regression tests for the disk space API endpoint.

GET /api/downloads/disk-space returns disk_free_bytes, disk_free_gb,
min_free_space_gb, is_low, and unavailable. is_low is True when
free_gb < min_free_space_gb. unavailable is True when the folder is
missing or disk_usage raises.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class DiskSpaceResponseTests(unittest.TestCase):
    """Verify the disk space endpoint returns correct fields and is_low flag."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_session(self, min_free_gb, folder="/tmp"):
        app_settings = MagicMock()
        app_settings.download_folder = folder
        app_settings.min_free_space_gb = min_free_gb

        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=app_settings)

        session = AsyncMock()
        session.execute = AsyncMock(return_value=scalar_mock)
        return session

    def _make_endpoint(self, free_bytes, min_free_gb, folder="/tmp"):
        from api.downloads import get_disk_space

        session = self._make_session(min_free_gb, folder)
        auth = MagicMock()

        disk_result = MagicMock()
        disk_result.free = free_bytes

        with patch("api.downloads.shutil.disk_usage", return_value=disk_result):
            with patch("api.downloads.os.path.exists", return_value=True):
                result = self._run(get_disk_space(_auth=auth, session=session))
        return result

    def test_response_has_required_fields(self):
        result = self._make_endpoint(free_bytes=50 * 1024 ** 3, min_free_gb=25)
        self.assertIn("disk_free_bytes", result)
        self.assertIn("disk_free_gb", result)
        self.assertIn("min_free_space_gb", result)
        self.assertIn("is_low", result)
        self.assertIn("unavailable", result)

    def test_is_low_false_when_plenty_of_space(self):
        result = self._make_endpoint(free_bytes=50 * 1024 ** 3, min_free_gb=25)
        self.assertFalse(result["is_low"])
        self.assertFalse(result["unavailable"])
        self.assertEqual(result["disk_free_gb"], 50.0)
        self.assertEqual(result["min_free_space_gb"], 25)

    def test_is_low_true_when_below_threshold(self):
        result = self._make_endpoint(free_bytes=10 * 1024 ** 3, min_free_gb=25)
        self.assertTrue(result["is_low"])
        self.assertFalse(result["unavailable"])
        self.assertEqual(result["disk_free_gb"], 10.0)

    def test_is_low_false_at_exact_threshold(self):
        result = self._make_endpoint(free_bytes=25 * 1024 ** 3, min_free_gb=25)
        self.assertFalse(result["is_low"])

    def test_free_gb_rounded_to_one_decimal(self):
        free_bytes = int(15.75 * 1024 ** 3)
        result = self._make_endpoint(free_bytes=free_bytes, min_free_gb=25)
        self.assertEqual(result["disk_free_gb"], round(15.75, 1))

    def test_folder_missing_returns_unavailable(self):
        from api.downloads import get_disk_space

        session = self._make_session(min_free_gb=25, folder="/nonexistent/path")
        auth = MagicMock()

        with patch("api.downloads.os.path.exists", return_value=False):
            result = self._run(get_disk_space(_auth=auth, session=session))

        self.assertTrue(result["unavailable"])
        self.assertFalse(result["is_low"])
        self.assertIsNone(result["disk_free_gb"])
        self.assertIsNone(result["disk_free_bytes"])
        self.assertEqual(result["min_free_space_gb"], 25)

    def test_disk_usage_error_returns_unavailable(self):
        from api.downloads import get_disk_space

        session = self._make_session(min_free_gb=25)
        auth = MagicMock()

        with patch("api.downloads.shutil.disk_usage", side_effect=PermissionError("denied")):
            with patch("api.downloads.os.path.exists", return_value=True):
                result = self._run(get_disk_space(_auth=auth, session=session))

        self.assertTrue(result["unavailable"])
        self.assertFalse(result["is_low"])
        self.assertIsNone(result["disk_free_gb"])

    def test_no_makedirs_side_effect_when_folder_missing(self):
        """GET /disk-space must not create folders on the filesystem."""
        from api.downloads import get_disk_space

        session = self._make_session(min_free_gb=25, folder="/nonexistent/path")
        auth = MagicMock()

        with patch("api.downloads.os.path.exists", return_value=False) as mock_exists:
            with patch("api.downloads.os.makedirs") as mock_makedirs:
                self._run(get_disk_space(_auth=auth, session=session))

        mock_makedirs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
