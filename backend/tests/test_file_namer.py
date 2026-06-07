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

    def test_all_dots_returns_fallback(self):
        # "..." strips to "" -> was producing hidden file ".ts"; must return fallback instead
        result = FileNamer.sanitize_filename("...")
        self.assertEqual(result, "unknown-program")

    def test_all_spaces_returns_fallback(self):
        result = FileNamer.sanitize_filename("   ")
        self.assertEqual(result, "unknown-program")

    def test_all_control_chars_returns_fallback(self):
        result = FileNamer.sanitize_filename("\x00\x00")
        self.assertEqual(result, "unknown-program")

    def test_dots_and_spaces_returns_fallback(self):
        result = FileNamer.sanitize_filename(".. ..")
        self.assertEqual(result, "unknown-program")

    def test_invisible_unicode_only_returns_fallback(self):
        # Zero-width space + zero-width non-joiner -> stripped -> fallback
        result = FileNamer.sanitize_filename("​‌")
        self.assertEqual(result, "unknown-program")

    def test_cjk_long_title_within_200_utf8_bytes(self):
        # 80 CJK chars × 3 bytes/char = 240 bytes; must be truncated to ≤200 bytes
        # Without the fix, 200-char limit passes but 240-byte filename causes ENAMETOOLONG
        title = "中" * 80
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)

    def test_ascii_200_char_limit_preserved(self):
        # ASCII: 1 byte/char, so 200-byte and 200-char limits are identical
        title = "A" * 250
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)
        self.assertEqual(len(result), 200)

    def test_mixed_ascii_cjk_within_200_utf8_bytes(self):
        # Mix of ASCII + CJK where total bytes would exceed 200 without the fix
        title = "Show: " + "中" * 70  # 6 + 210 = 216 bytes
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)


if __name__ == "__main__":
    unittest.main()
