"""
Regression test: _cleanup_working_files must not delete user subtitle/metadata files.

Bug: download_manager._cleanup_working_files included .xml, .srt, .ass, .vtt in its
cleanup pattern list. On a normal ComSkip + transcode completion the completed-download
path calls this helper, silently deleting subtitle files that users had placed alongside
their recordings.

Fix: those four extensions are removed from the pattern list. Only actual ComSkip/FFmpeg
working files (.edl, .txt, .logo, .csv, .vdr, _seg*.ts, .concat.txt, .log) are removed.
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


class CleanupWorkingFilesSubtitleTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def _make(self, name: str) -> Path:
        p = Path(self.tmp) / name
        p.write_text("dummy")
        return p

    def test_subtitle_files_survive_completed_download_cleanup(self):
        """User subtitle files next to a completed recording must not be deleted."""
        original = self._make("Show.S01E01.ts")
        completed = self._make("Show.S01E01.mkv")
        srt = self._make("Show.S01E01.srt")
        vtt = self._make("Show.S01E01.vtt")
        ass = self._make("Show.S01E01.ass")
        xml = self._make("Show.S01E01.xml")

        self.manager._cleanup_working_files(
            str(original), str(completed), keep_logs=True, delete_original=False
        )

        self.assertTrue(srt.exists(), ".srt must not be deleted (user subtitle)")
        self.assertTrue(vtt.exists(), ".vtt must not be deleted (user subtitle)")
        self.assertTrue(ass.exists(), ".ass must not be deleted (user subtitle)")
        self.assertTrue(xml.exists(), ".xml must not be deleted (user metadata)")

    def test_comskip_working_files_still_cleaned(self):
        """ComSkip/FFmpeg working files are still removed by _cleanup_working_files."""
        original = self._make("Show.ts")
        completed = self._make("Show.mkv")
        edl = self._make("Show.edl")
        txt = self._make("Show.txt")
        logo = self._make("Show.logo")
        csv = self._make("Show.csv")
        vdr = self._make("Show.vdr")
        seg = self._make("Show_seg001.ts")
        concat = self._make("Show.concat.txt")

        self.manager._cleanup_working_files(
            str(original), str(completed), keep_logs=True, delete_original=False
        )

        for f, name in [
            (edl, ".edl"), (txt, ".txt"), (logo, ".logo"),
            (csv, ".csv"), (vdr, ".vdr"), (seg, "_seg*.ts"), (concat, ".concat.txt"),
        ]:
            self.assertFalse(f.exists(), f"{name} should be cleaned up")

    def test_log_cleaned_when_keep_logs_false(self):
        """.log is removed when keep_logs=False."""
        original = self._make("Show.ts")
        completed = self._make("Show.mkv")
        log = self._make("Show.log")

        self.manager._cleanup_working_files(
            str(original), str(completed), keep_logs=False, delete_original=False
        )

        self.assertFalse(log.exists(), ".log should be cleaned up when keep_logs=False")

    def test_log_survives_when_keep_logs_true(self):
        """.log is kept when keep_logs=True."""
        original = self._make("Show.ts")
        completed = self._make("Show.mkv")
        log = self._make("Show.log")

        self.manager._cleanup_working_files(
            str(original), str(completed), keep_logs=True, delete_original=False
        )

        self.assertTrue(log.exists(), ".log must not be deleted when keep_logs=True")


if __name__ == "__main__":
    unittest.main()
