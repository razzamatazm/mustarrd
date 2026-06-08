"""
Regression test: series_episode_output_path must produce a filename whose
basename does not exceed Linux NAME_MAX (255 bytes).

When both show name and episode title are long UTF-8 strings,
series_episode_output_path combines two independently-capped 200-byte
components without truncating the result.  The combined filename can reach
400+ bytes, causing OSError: [Errno 36] File name too long when the download
manager tries to open the output file, marking the download FAILED with a
raw OS error that is not actionable by a non-technical operator.

File:     backend/services/vod_namer.py, series_episode_output_path
Severity: MEDIUM -- affects providers serving CJK-titled series or any series
          with show names > ~85 bytes combined with episode titles > ~145 bytes.
"""
import os
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.vod_namer import series_episode_output_path

NAME_MAX = 255


class SeriesEpisodeNameMaxTests(unittest.TestCase):

    def _filename_bytes(self, path: str) -> int:
        return len(os.path.basename(path).encode("utf-8"))

    def test_long_cjk_show_and_title_within_name_max(self):
        """
        Korean show name (195 bytes) + Korean episode title (189 bytes) must
        produce a filename that fits in 255 bytes.  Before the fix the combined
        S01E01 prefix + show + separator + title + extension reached 400 bytes,
        causing [Errno 36] File name too long at download time.
        """
        long_show = "한국드라마" * 13    # 65 Korean chars = 195 UTF-8 bytes
        long_title = "에피소드타이틀" * 9  # 63 Korean chars = 189 UTF-8 bytes
        path = series_episode_output_path("/downloads", long_show, 1, 1, long_title, "mp4")
        byte_len = self._filename_bytes(path)
        self.assertLessEqual(
            byte_len,
            NAME_MAX,
            f"Filename is {byte_len} bytes, exceeds NAME_MAX ({NAME_MAX}). "
            f"Attempting to write to this path raises "
            f"OSError: [Errno 36] File name too long.",
        )

    def test_long_ascii_show_and_title_within_name_max(self):
        """
        ASCII show name (100 bytes) + ASCII episode title (200 bytes) must also
        fit within NAME_MAX.  Before the fix the combined filename was 316 bytes.
        """
        long_show = "S" * 100
        long_title = "T" * 200
        path = series_episode_output_path("/downloads", long_show, 1, 1, long_title, "mp4")
        byte_len = self._filename_bytes(path)
        self.assertLessEqual(
            byte_len,
            NAME_MAX,
            f"Filename is {byte_len} bytes, exceeds NAME_MAX ({NAME_MAX}). "
            f"Attempting to write to this path raises "
            f"OSError: [Errno 36] File name too long.",
        )

    def test_long_show_no_title_within_name_max(self):
        """
        A long show name without an episode title should remain within NAME_MAX.
        (S01E01 + 200-byte show + .mp4 = 216 bytes; already passing before the fix.)
        Included to confirm short-title case is not regressed by any truncation fix.
        """
        long_show = "한국드라마" * 13
        path = series_episode_output_path("/downloads", long_show, 1, 1, None, "mp4")
        byte_len = self._filename_bytes(path)
        self.assertLessEqual(
            byte_len,
            NAME_MAX,
            f"Filename (no episode title) is {byte_len} bytes, exceeds NAME_MAX ({NAME_MAX}).",
        )


if __name__ == "__main__":
    unittest.main()
