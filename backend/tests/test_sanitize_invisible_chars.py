"""
Regression test: sanitize_filename must strip invisible Unicode format/directional
characters injected by IPTV providers into channel names and EPG titles.

Currently failing: these chars survive sanitization and appear in filenames on disk,
breaking Plex/Jellyfin library matching and making files hard to find or delete.

Characters tested:
  U+FEFF  ZERO WIDTH NO-BREAK SPACE (BOM) -- appears at start of UTF-8 streams
  U+200B  ZERO WIDTH SPACE                -- injected between words by some providers
  U+200C  ZERO WIDTH NON-JOINER          -- used in Arabic/Persian text shaping
  U+200D  ZERO WIDTH JOINER              -- used in emoji sequences, injected by providers
  U+200E  LEFT-TO-RIGHT MARK             -- RTL providers inject this in mixed content
  U+200F  RIGHT-TO-LEFT MARK             -- RTL providers inject this in mixed content
  U+00AD  SOFT HYPHEN                    -- invisible in most renderers, causes name mismatches
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import FileNamer

INVISIBLE_CHARS = {
    "BOM (U+FEFF)": "﻿",
    "zero-width space (U+200B)": "​",
    "zero-width non-joiner (U+200C)": "‌",
    "zero-width joiner (U+200D)": "‍",
    "LTR mark (U+200E)": "‎",
    "RTL mark (U+200F)": "‏",
    "soft hyphen (U+00AD)": "­",
}


class SanitizeInvisibleCharsTests(unittest.TestCase):
    def test_each_invisible_char_stripped(self):
        for label, char in INVISIBLE_CHARS.items():
            with self.subTest(char=label):
                name = f"Breaking{char}Bad"
                result = FileNamer.sanitize_filename(name)
                self.assertNotIn(
                    char,
                    result,
                    f"{label} survived sanitize_filename: {repr(result)}",
                )

    def test_bom_only_title_does_not_crash(self):
        result = FileNamer.sanitize_filename("﻿")
        self.assertNotIn("﻿", result)

    def test_mixed_invisible_chars_stripped(self):
        name = "﻿Breaking​ Bad‎ S01E01‏"
        result = FileNamer.sanitize_filename(name)
        for char in INVISIBLE_CHARS.values():
            self.assertNotIn(char, result, f"Invisible char {repr(char)} survived: {repr(result)}")

    def test_rtl_provider_channel_name(self):
        name = "‏Al Jazeera‏ English​"
        result = FileNamer.sanitize_filename(name)
        for char in INVISIBLE_CHARS.values():
            self.assertNotIn(char, result, f"Invisible char {repr(char)} survived: {repr(result)}")
        self.assertIn("Al Jazeera", result)


if __name__ == "__main__":
    unittest.main()
