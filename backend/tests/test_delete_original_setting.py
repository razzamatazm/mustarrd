"""
Regression test: delete_original_after_transcode=False must preserve the original
.ts file after post-processing completes.

Currently failing: _cleanup_working_files in download_manager.py unconditionally
deletes original_path whenever original_path != completed_path, regardless of the
delete_original_after_transcode setting. The Settings page exposes this toggle
(Settings.jsx line 777), but it has no effect — the original is always deleted.

Reproduction:
  1. Set delete_original_after_transcode = False in Settings.
  2. Download a catchup recording (ffmpeg/comskip post-processing runs).
  3. Check the download folder: the original .ts file is gone despite the setting.

Expected: original .ts survives when delete_original_after_transcode=False.
Actual:   original .ts is always deleted by _cleanup_working_files.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


class DeleteOriginalSettingTests(unittest.TestCase):
    def setUp(self):
        self.dm = DownloadManager()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_files(self, original_name: str, completed_name: str) -> tuple[str, str]:
        original = os.path.join(self.tmp, "downloads", original_name)
        completed = os.path.join(self.tmp, "completed", completed_name)
        os.makedirs(os.path.dirname(original), exist_ok=True)
        os.makedirs(os.path.dirname(completed), exist_ok=True)
        Path(original).write_bytes(b"raw ts content")
        Path(completed).write_bytes(b"transcoded mkv content")
        return original, completed

    def test_original_preserved_when_delete_original_false(self):
        """Original .ts must survive _cleanup_working_files when the user chose to keep it.

        This test currently FAILS because _cleanup_working_files has no parameter to
        control whether the original should be deleted, so it always removes it.
        The fix: accept a delete_original keyword and skip the unlink when False.
        """
        original, completed = self._make_files("Show S01E01.ts", "Show S01E01.mkv")

        # Simulate the call from _execute_post_process with delete_original=False.
        # After the fix adds the parameter, this call must preserve the original.
        # Currently FAILS because the parameter does not exist and cleanup always deletes.
        self.dm._cleanup_working_files(original, completed, keep_logs=False, delete_original=False)

        self.assertTrue(
            os.path.exists(original),
            "Original .ts was deleted even though delete_original_after_transcode=False. "
            "_cleanup_working_files must respect the delete_original_after_transcode setting.",
        )

    def test_original_deleted_when_delete_original_true(self):
        """Sanity: original .ts must be deleted when delete_original_after_transcode=True.

        This test currently PASSES (current behaviour).  It exists so the fix does not
        break the default/True path.
        """
        original, completed = self._make_files("Show S01E02.ts", "Show S01E02.mkv")

        # For now the only call signature available always deletes.
        # After the fix, passing delete_original=True should produce the same result.
        self.dm._cleanup_working_files(original, completed, keep_logs=False)

        self.assertFalse(
            os.path.exists(original),
            "Original .ts should be deleted when delete_original_after_transcode=True.",
        )

    def test_cleanup_working_files_accepts_delete_original_parameter(self):
        """_cleanup_working_files must expose a delete_original keyword parameter.

        This test currently FAILS because the parameter does not exist in the
        current signature: _cleanup_working_files(original, completed, keep_logs).
        After the fix, calling it with delete_original=False must not raise TypeError.
        """
        original, completed = self._make_files("Show S01E03.ts", "Show S01E03.mkv")

        try:
            self.dm._cleanup_working_files(
                original, completed, keep_logs=False, delete_original=False
            )
        except TypeError as exc:
            self.fail(
                f"_cleanup_working_files does not accept delete_original parameter: {exc}"
            )


if __name__ == "__main__":
    unittest.main()
