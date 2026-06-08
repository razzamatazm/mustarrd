"""
Regression test: _escape_concat_path must produce paths that round-trip correctly
through ffmpeg's concat demuxer parser (av_get_token in libavutil/avstring.c).

The concat file line format is: file '<escaped_path>'
ffmpeg parses this with av_get_token rules:
  - Inside single-quoted spans: characters are taken literally (no backslash processing).
  - Outside single-quoted spans: backslash escapes the next character.

To embed a literal ' in a single-quoted path, the sequence must be: '<before>'\''<after>'
(close quote, backslash-escaped quote outside quotes, reopen quote). This is the
documented ffmpeg form (e.g. "Crime d'\\'Amour" in the ffmpeg utils docs and
"file '/mnt/share/file 3'\\''.wav'" in the concat demuxer docs).

Using backslash-quote (\\') inside a single-quoted span does NOT work: ffmpeg treats
backslash as a literal character inside single quotes, so the apostrophe closes the
quoted span and the path is truncated. Example:
  file 'Father\\'s Day.ts'  -> ffmpeg reads 'Father\\' then stops at the apostrophe

Effect if wrong: commercial removal via the concat+encode path fails for any recording
whose filename contains an apostrophe (e.g., "Father's Day", "New Year's Eve",
"Britain's Got Talent"). ffmpeg receives a truncated path and errors with
"No such file or directory". The recording is marked FAILED with no output file.

sanitize_filename does not strip single quotes (invalid_chars = r'[<>:"/\\\\|?*\\x00-\\x1f]'
excludes '), so provider titles with apostrophes pass through to segment filenames.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MODULE_PATH = BACKEND_ROOT / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_concat_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


def _av_get_token(token: str) -> str:
    """
    Reference implementation of ffmpeg av_get_token for concat file paths.

    Rules (from libavutil/avstring.c):
    - Single-quoted span (entered/exited by '): characters taken literally,
      no backslash processing inside.
    - Outside single-quoted spans: backslash escapes the next character.
    """
    result = []
    i = 0
    while i < len(token):
        c = token[i]
        if c == "'":
            i += 1
            while i < len(token) and token[i] != "'":
                result.append(token[i])
                i += 1
            if i < len(token):
                i += 1  # consume closing '
        elif c == "\\":
            i += 1
            if i < len(token):
                result.append(token[i])
                i += 1
        else:
            result.append(c)
            i += 1
    return "".join(result)


class ConcatPathEscapeTests(unittest.TestCase):
    """_escape_concat_path must produce valid ffmpeg concat format paths."""

    def setUp(self):
        self.processor = PostProcessor()

    def _parse_concat_path(self, path: Path) -> str:
        """Escape path and parse back through the av_get_token reference parser."""
        escaped = self.processor._escape_concat_path(path)
        return _av_get_token(f"'{escaped}'")

    def test_apostrophe_roundtrips_fathers_day(self):
        """Apostrophe in filename must survive the escape/parse round-trip.

        'Father\\'s Day' is the documented failure case: ffmpeg reads only
        'Father\\' when backslash-quote is used inside a single-quoted span.
        The correct escape is the close-quote/backslash-quote/reopen sequence.
        """
        path = Path("/downloads/Father's Day_seg0.ts")
        parsed = self._parse_concat_path(path)
        self.assertEqual(
            parsed,
            str(path),
            f"Path did not round-trip through av_get_token. "
            f"escaped={self.processor._escape_concat_path(path)!r}, parsed={parsed!r}",
        )

    def test_apostrophe_roundtrips_new_years_eve(self):
        """Apostrophe round-trip for a multi-word title with season/episode suffix."""
        path = Path("/downloads/New Year's Eve S01E01_seg0.ts")
        parsed = self._parse_concat_path(path)
        self.assertEqual(
            parsed,
            str(path),
            f"Path did not round-trip. "
            f"escaped={self.processor._escape_concat_path(path)!r}, parsed={parsed!r}",
        )

    def test_apostrophe_roundtrips_britains_got_talent(self):
        """Apostrophe round-trip for a show title with apostrophe mid-word."""
        path = Path("/downloads/Britain's Got Talent S12E03_seg0.ts")
        parsed = self._parse_concat_path(path)
        self.assertEqual(
            parsed,
            str(path),
            f"Path did not round-trip. "
            f"escaped={self.processor._escape_concat_path(path)!r}, parsed={parsed!r}",
        )

    def test_path_without_special_chars_unchanged(self):
        """Paths without single quotes must not be modified."""
        path = Path("/downloads/Breaking Bad S01E01_seg0.ts")
        escaped = self.processor._escape_concat_path(path)
        self.assertEqual(escaped, str(path))

    def test_backslash_in_path_doubled(self):
        """Backslashes in paths must be doubled as required by ffmpeg concat format."""
        path = Path("/downloads/Show\\Name_seg0.ts")
        escaped = self.processor._escape_concat_path(path)
        self.assertIn("\\\\", escaped)


if __name__ == "__main__":
    unittest.main()
