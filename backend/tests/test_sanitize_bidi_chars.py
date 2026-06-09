"""
Regression test: sanitize_filename must strip Unicode bidi override and isolate
controls injected by IPTV providers into channel names and EPG titles.

These characters cause filenames to display reversed or mangled in terminals and
file managers. U+202E (RTL Override) is the most dangerous: a file named
"show‮ts.gpj" appears as "show.jpg" in some terminals while the real
extension is .ts.gpj (classic "trojan filename" trick).

Characters tested:
  U+202A  LEFT-TO-RIGHT EMBEDDING
  U+202B  RIGHT-TO-LEFT EMBEDDING
  U+202C  POP DIRECTIONAL FORMATTING
  U+202D  LEFT-TO-RIGHT OVERRIDE
  U+202E  RIGHT-TO-LEFT OVERRIDE  -- reverses text display in many terminals
  U+2066  LEFT-TO-RIGHT ISOLATE
  U+2067  RIGHT-TO-LEFT ISOLATE
  U+2068  FIRST STRONG ISOLATE
  U+2069  POP DIRECTIONAL ISOLATE
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import FileNamer

BIDI_CHARS = {
    "LTR Embedding (U+202A)": "‪",
    "RTL Embedding (U+202B)": "‫",
    "Pop Directional Formatting (U+202C)": "‬",
    "LTR Override (U+202D)": "‭",
    "RTL Override (U+202E)": "‮",
    "LTR Isolate (U+2066)": "⁦",
    "RTL Isolate (U+2067)": "⁧",
    "First Strong Isolate (U+2068)": "⁨",
    "Pop Directional Isolate (U+2069)": "⁩",
}


class SanitizeBidiCharsTests(unittest.TestCase):
    def test_each_bidi_char_stripped(self):
        for label, char in BIDI_CHARS.items():
            with self.subTest(char=label):
                name = f"Breaking{char}Bad"
                result = FileNamer.sanitize_filename(name)
                self.assertNotIn(
                    char,
                    result,
                    f"{label} survived sanitize_filename: {repr(result)}",
                )

    def test_rtl_override_trojan_filename_cleaned(self):
        # RTL Override makes "ts.gpj" render as "jpg.st" in terminals.
        # The filename must come out clean with no bidi controls.
        name = "My Show S01E01‮ts.gpj"
        result = FileNamer.sanitize_filename(name)
        self.assertNotIn("‮", result)
        self.assertIn("My Show S01E01", result)

    def test_mixed_bidi_chars_all_stripped(self):
        name = "‪Breaking‮ Bad⁦ S01E01⁩"
        result = FileNamer.sanitize_filename(name)
        for char in BIDI_CHARS.values():
            self.assertNotIn(char, result, f"Bidi char {repr(char)} survived: {repr(result)}")
        self.assertIn("Breaking", result)

    def test_bidi_only_title_returns_fallback(self):
        result = FileNamer.sanitize_filename("‮⁦⁩")
        self.assertEqual(result, "unknown-program")


if __name__ == "__main__":
    unittest.main()
