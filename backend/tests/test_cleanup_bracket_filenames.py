"""
Regression test: _cleanup_working_files must delete ComSkip temp files
even when the filename stem contains glob special characters like '[' and ']'.

Common real-world IPTV channel names include brackets: [BBC], [HD], [US].
sanitize_filename does not strip '[' or ']' (they are valid Linux filename chars),
so stems like '[BBC] Doctor Who - 2024-01-15' reach _cleanup_working_files.

Path.glob() treats '[...]' as a character class, so the cleanup patterns silently
fail to match their targets, leaving ComSkip temp files on disk indefinitely.
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
from services.file_namer import file_namer


class CleanupBracketFilenamesTest(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_sanitize_preserves_brackets(self):
        """sanitize_filename must leave '[' and ']' in the output (they are valid Linux chars)."""
        result = file_namer.sanitize_filename("[BBC] Doctor Who - S01E01")
        self.assertIn("[", result, "sanitize_filename strips '['; test precondition changed")
        self.assertIn("]", result, "sanitize_filename strips ']'; test precondition changed")

    def test_cleanup_removes_comskip_files_when_stem_contains_brackets(self):
        """ComSkip temp files are deleted even when the stem contains '[' or ']'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stem = "[BBC] Doctor Who - 2024-01-15"
            ts_file = os.path.join(tmpdir, f"{stem}.ts")
            completed_path = os.path.join(tmpdir, f"{stem}.mkv")

            Path(ts_file).write_bytes(b"\x00")

            temp_files = {
                "edl":  Path(tmpdir) / f"{stem}.edl",
                "txt":  Path(tmpdir) / f"{stem}.txt",
                "log":  Path(tmpdir) / f"{stem}.log",
                "csv":  Path(tmpdir) / f"{stem}.csv",
                "logo": Path(tmpdir) / f"{stem}.logo",
                "vdr":  Path(tmpdir) / f"{stem}.vdr",
            }
            for f in temp_files.values():
                f.write_text("placeholder")

            seg_file = Path(tmpdir) / f"{stem}_seg001.ts"
            seg_file.write_text("segment")

            self.manager._cleanup_working_files(
                original_path=ts_file,
                completed_path=completed_path,
                keep_logs=False,
                delete_original=True,
            )

            for label, path in temp_files.items():
                self.assertFalse(
                    path.exists(),
                    f"ComSkip temp file '{label}' was not cleaned up for stem with brackets: {path.name}",
                )

            self.assertFalse(
                seg_file.exists(),
                f"Segment file was not cleaned up for stem with brackets: {seg_file.name}",
            )

    def test_cleanup_does_not_delete_unrelated_files_with_similar_names(self):
        """Cleanup must not accidentally delete unrelated files that happen to match a mangled glob."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stem = "[BBC] Show - 2024-01-15"
            ts_file = os.path.join(tmpdir, f"{stem}.ts")
            completed_path = os.path.join(tmpdir, f"{stem}.mkv")
            Path(ts_file).write_bytes(b"\x00")

            # '[BBC]' as a broken glob character class matches 'B' or 'C', so a file
            # starting with 'B' followed by ' Show - 2024-01-15.edl' would be incorrectly
            # deleted without glob.escape().
            unrelated = Path(tmpdir) / "B Show - 2024-01-15.edl"
            unrelated.write_text("unrelated file -- must not be deleted")

            self.manager._cleanup_working_files(
                original_path=ts_file,
                completed_path=completed_path,
                keep_logs=False,
                delete_original=True,
            )

            self.assertTrue(
                unrelated.exists(),
                "Cleanup deleted an unrelated file because of bad glob pattern escaping",
            )


if __name__ == "__main__":
    unittest.main()
