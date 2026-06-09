"""
Regression test: empty Show/Season directories must be removed from the
download folder after a series episode is moved to the completed folder.

Bug: series episodes are written to Show/Season subfolders of the download
(working) folder. _move_to_completed moved the file out (preserving the
relative path under the completed folder) but left the now-empty Show/Season
directories behind, so the download folder accumulated empty directory trees
over time.

Fix: after a successful move, _move_to_completed removes empty parent
directories up to — but never including — the configured download folder.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


class MoveToCompletedPrunesEmptyDirsTests(unittest.TestCase):

    def setUp(self):
        self.manager = DownloadManager()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.download_dir = Path(self.tmp) / "downloads"
        self.completed_dir = Path(self.tmp) / "completed"
        self.download_dir.mkdir()
        self.completed_dir.mkdir()

    def _make_episode(self, *parts: str) -> Path:
        path = self.download_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x47" * 188)
        return path

    def test_empty_show_and_season_dirs_removed_after_move(self):
        episode = self._make_episode("My Show", "Season 01", "My Show S01E01.ts")

        dest = self.manager._move_to_completed(
            str(episode), str(self.completed_dir), str(self.download_dir)
        )

        self.assertTrue(os.path.isfile(dest), "Episode must be moved to completed folder.")
        self.assertEqual(
            dest,
            str(self.completed_dir / "My Show" / "Season 01" / "My Show S01E01.ts"),
            "Relative Show/Season structure must be preserved in completed folder.",
        )
        self.assertFalse(
            (self.download_dir / "My Show" / "Season 01").exists(),
            "Empty Season directory must be removed from the download folder.",
        )
        self.assertFalse(
            (self.download_dir / "My Show").exists(),
            "Empty Show directory must be removed from the download folder.",
        )
        self.assertTrue(
            self.download_dir.exists(),
            "The configured download folder itself must never be removed.",
        )

    def test_non_empty_dirs_are_kept(self):
        episode = self._make_episode("My Show", "Season 01", "My Show S01E01.ts")
        sibling = self._make_episode("My Show", "Season 01", "My Show S01E02.ts")

        self.manager._move_to_completed(
            str(episode), str(self.completed_dir), str(self.download_dir)
        )

        self.assertTrue(sibling.exists(), "Sibling episode must not be touched.")
        self.assertTrue(
            (self.download_dir / "My Show" / "Season 01").exists(),
            "A Season directory that still has files must be kept.",
        )

    def test_file_directly_in_download_folder_keeps_root(self):
        episode = self._make_episode("Movie.ts")

        self.manager._move_to_completed(
            str(episode), str(self.completed_dir), str(self.download_dir)
        )

        self.assertTrue(
            self.download_dir.exists(),
            "The download folder must survive moving a file that lives directly in it.",
        )

    def test_path_outside_download_folder_prunes_nothing(self):
        outside_dir = Path(self.tmp) / "elsewhere" / "nested"
        outside_dir.mkdir(parents=True)
        episode = outside_dir / "Movie.ts"
        episode.write_bytes(b"\x47" * 188)

        self.manager._move_to_completed(
            str(episode), str(self.completed_dir), str(self.download_dir)
        )

        self.assertTrue(
            outside_dir.exists(),
            "Directories outside the download folder must never be pruned.",
        )


if __name__ == "__main__":
    unittest.main()
