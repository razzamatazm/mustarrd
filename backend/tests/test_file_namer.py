import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import FileNamer


class SanitizeFilenameTests(unittest.TestCase):
    def test_strips_null_byte(self):
        result = FileNamer.sanitize_filename("The Crown\x00S01E01")
        self.assertNotIn("\x00", result)
        self.assertEqual(result, "The Crown S01E01")

    def test_strips_other_control_chars(self):
        # Tab (\x09), carriage return (\x0d), and other control chars should be removed
        result = FileNamer.sanitize_filename("Show\x09Name\x0dEpisode\x1fTitle")
        for cp in range(0x00, 0x20):
            self.assertNotIn(chr(cp), result)

    def test_null_byte_mid_title_collapses_space(self):
        # Multiple adjacent control chars should collapse to a single space
        result = FileNamer.sanitize_filename("Show\x00\x00Name")
        self.assertEqual(result, "Show Name")

    def test_normal_title_unchanged(self):
        result = FileNamer.sanitize_filename("Breaking Bad S01E01")
        self.assertEqual(result, "Breaking Bad S01E01")

    def test_windows_invalid_chars_still_stripped(self):
        result = FileNamer.sanitize_filename('Bad:Title/Name\\Here?')
        self.assertNotIn(':', result)
        self.assertNotIn('/', result)
        self.assertNotIn('\\', result)
        self.assertNotIn('?', result)


if __name__ == "__main__":
    unittest.main()
