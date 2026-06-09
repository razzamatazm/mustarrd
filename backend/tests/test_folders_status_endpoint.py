"""
Regression tests for GET /api/settings/folders/status.

The endpoint reports the resolved download and completed folder paths, whether
each exists, and the result of a real write test (create + delete a temp file).
Failure reasons distinguish a missing mount, a read-only filesystem, and a
permission problem so the Settings page can tell the user what to fix.
"""
import errno
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.settings import get_folders_status, _probe_folder_writable
from models import AppSettings


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _make_session(settings_row):
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(settings_row))
    return session


class ProbeFolderWritableTests(unittest.TestCase):
    def test_existing_writable_folder_passes_write_test(self):
        with tempfile.TemporaryDirectory() as folder:
            info = _probe_folder_writable(folder)
        self.assertEqual(info["path"], folder)
        self.assertTrue(info["exists"])
        self.assertTrue(info["writable"])
        self.assertIsNone(info["error"])

    def test_write_test_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as folder:
            _probe_folder_writable(folder)
            self.assertEqual(
                os.listdir(folder),
                [],
                "The write test must delete its temp file.",
            )

    def test_missing_folder_reports_missing_mount(self):
        missing = os.path.join(tempfile.gettempdir(), "mustarrd-definitely-missing-xyz")
        info = _probe_folder_writable(missing)
        self.assertFalse(info["exists"])
        self.assertFalse(info["writable"])
        self.assertIn("does not exist", info["error"])
        self.assertIn("mount", info["error"])

    def test_empty_path_reports_not_configured(self):
        info = _probe_folder_writable("")
        self.assertFalse(info["exists"])
        self.assertFalse(info["writable"])
        self.assertIn("configured", info["error"])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_permission_denied_reports_permission_error(self):
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)  # r-x: listable, not writable
            try:
                info = _probe_folder_writable(folder)
            finally:
                os.chmod(folder, stat.S_IRWXU)
        self.assertTrue(info["exists"])
        self.assertFalse(info["writable"])
        self.assertIn("permission", info["error"].lower())

    def test_read_only_filesystem_reports_read_only(self):
        """EROFS (e.g. a read-only NFS/SMB mount) must produce a read-only reason."""
        with tempfile.TemporaryDirectory() as folder:
            with patch(
                "api.settings.tempfile.mkstemp",
                side_effect=OSError(errno.EROFS, "Read-only file system"),
            ):
                info = _probe_folder_writable(folder)
        self.assertTrue(info["exists"])
        self.assertFalse(info["writable"])
        self.assertIn("read-only", info["error"].lower())

    def test_disk_full_reports_disk_full(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch(
                "api.settings.tempfile.mkstemp",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ):
                info = _probe_folder_writable(folder)
        self.assertTrue(info["exists"])
        self.assertFalse(info["writable"])
        self.assertIn("full", info["error"].lower())


class FoldersStatusEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_both_resolved_folders(self):
        """Endpoint returns the DB-configured folders with write-test results."""
        with tempfile.TemporaryDirectory() as download_dir, tempfile.TemporaryDirectory() as completed_dir:
            settings_row = AppSettings()
            settings_row.download_folder = download_dir
            settings_row.completed_folder = completed_dir

            result = await get_folders_status(None, _make_session(settings_row))

        self.assertEqual(result["download_folder"]["path"], download_dir)
        self.assertEqual(result["completed_folder"]["path"], completed_dir)
        for key in ("download_folder", "completed_folder"):
            self.assertTrue(result[key]["exists"])
            self.assertTrue(result[key]["writable"])
            self.assertIsNone(result[key]["error"])

    async def test_missing_completed_folder_flagged_with_reason(self):
        """A disconnected completed-folder mount is reported, download folder stays OK."""
        with tempfile.TemporaryDirectory() as download_dir:
            settings_row = AppSettings()
            settings_row.download_folder = download_dir
            settings_row.completed_folder = os.path.join(download_dir, "gone", "completed")

            result = await get_folders_status(None, _make_session(settings_row))

        self.assertTrue(result["download_folder"]["writable"])
        self.assertFalse(result["completed_folder"]["exists"])
        self.assertFalse(result["completed_folder"]["writable"])
        self.assertIn("does not exist", result["completed_folder"]["error"])

    async def test_no_settings_row_falls_back_to_env_defaults(self):
        """With no AppSettings row, the endpoint resolves the env-default folders."""
        with tempfile.TemporaryDirectory() as default_dir:
            with patch("services.download_manager.app_settings") as mock_cfg:
                mock_cfg.default_download_folder = default_dir
                mock_cfg.default_completed_folder = default_dir

                result = await get_folders_status(None, _make_session(None))

        self.assertEqual(result["download_folder"]["path"], default_dir)
        self.assertEqual(result["completed_folder"]["path"], default_dir)
        self.assertTrue(result["download_folder"]["writable"])


if __name__ == "__main__":
    unittest.main()
