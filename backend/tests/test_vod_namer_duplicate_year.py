"""
Regression test: movie_output_path must not duplicate the year when the
provider already includes it in the title.

Providers commonly format VOD titles as "Movie Name (YYYY)".  When
movie_output_path receives that title alongside a year argument (which
comes from release_date or extracted from the title itself), it appends
"(YYYY)" a second time, producing "Movie Name (YYYY) (YYYY).mp4".

Expected: "Movie Name (YYYY).mp4"
Actual (before fix): "Movie Name (YYYY) (YYYY).mp4"
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.vod_namer import movie_output_path


class MovieOutputPathDuplicateYearTests(unittest.TestCase):

    def test_no_duplicate_year_when_title_already_contains_year(self):
        """Title 'Inception (2010)' with year=2010 must not produce '(2010) (2010)'."""
        result = movie_output_path("/downloads", "Inception (2010)", 2010, "mkv")
        basename = Path(result).name
        self.assertNotIn("(2010) (2010)", basename,
                         f"Year duplicated in filename: {basename!r}")
        self.assertIn("(2010)", basename,
                      f"Year missing entirely from filename: {basename!r}")

    def test_no_duplicate_year_with_release_date_and_title_year(self):
        """'Terminator 2: Judgment Day (1991)' + year=1991 must not double-year."""
        result = movie_output_path(
            "/downloads", "Terminator 2: Judgment Day (1991)", 1991, "mp4"
        )
        basename = Path(result).name
        count = basename.count("(1991)")
        self.assertEqual(count, 1,
                         f"Expected exactly one '(1991)' in filename, got {count}: {basename!r}")

    def test_no_duplicate_year_common_iptv_title_format(self):
        """'The Matrix (1999)' title with extracted year=1999 must not double-year."""
        result = movie_output_path("/downloads", "The Matrix (1999)", 1999, "mp4")
        basename = Path(result).name
        self.assertNotIn("(1999) (1999)", basename,
                         f"Year duplicated in filename: {basename!r}")

    def test_year_appended_when_not_in_title(self):
        """When title has no year, year suffix is still added normally."""
        result = movie_output_path("/downloads", "Inception", 2010, "mkv")
        basename = Path(result).name
        self.assertIn("(2010)", basename,
                      f"Year should be appended when title has none: {basename!r}")
        count = basename.count("(2010)")
        self.assertEqual(count, 1, f"Year should appear exactly once: {basename!r}")

    def test_no_year_suffix_when_year_is_none(self):
        """When year=None, no year suffix is appended."""
        result = movie_output_path("/downloads", "Inception", None, "mkv")
        basename = Path(result).name
        self.assertNotIn("(", basename,
                         "Unexpected parentheses when year is None")
        import re
        self.assertIsNone(re.search(r'\(\d{4}\)', basename),
                          f"Unexpected year in filename: {basename!r}")


if __name__ == "__main__":
    unittest.main()
