"""
Regression test: _escape_concat_path uses shell-style single-quote escaping ("'\\''")
which is invalid in ffmpeg concat format.

Within a single-quoted ffmpeg concat path, a literal single quote must be escaped as
\\' (backslash-quote). Shell-style quoting closes and reopens the surrounding quotes,
which ffmpeg's concat parser does not support -- it terminates the quoted string at the
first unescaped '.

Effect: commercial removal via the concat+encode path fails for any recording whose
filename contains an apostrophe (e.g., "Father's Day", "New Year's Eve", "Britain's
Got Talent"). ffmpeg receives only the partial path before the apostrophe as the
filename, then errors out.

sanitize_filename does not strip single quotes (invalid_chars = r'[<>:"/\\\\|?*\\x00-\\x1f]'
excludes '), so provider titles like "It's a Wonderful Life" pass through to segment
temp filenames unchanged.

Reproduction:
  1. Enable ComSkip + commercial removal in Settings.
  2. Schedule a recording whose EPG title contains an apostrophe.
  3. After download completes, observe the post-processor log: ffmpeg concat step
     fails with "No such file or directory" for the truncated filename.
  4. Recording is marked FAILED / WARNING; no output file is produced.

Expected: apostrophe in filename escaped as \\' so ffmpeg concat line is valid.
Actual:   apostrophe escaped as '\\'' producing a split ffmpeg concat token.
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


class ConcatPathEscapeTests(unittest.TestCase):
    """_escape_concat_path must produce valid ffmpeg concat format, not shell quoting."""

    def setUp(self):
        self.processor = PostProcessor()

    def test_apostrophe_not_shell_style_escaped(self):
        """Single quote must not use shell-style \\'\\'' escaping in ffmpeg concat path.

        Shell-style (\\'\\'' ) closes the surrounding single-quoted string, emits a raw
        apostrophe, then reopens it. ffmpeg concat parser does not support this -- it
        terminates the path at the first unescaped quote.
        """
        path = Path("/downloads/Father's Day_seg0.ts")
        escaped = self.processor._escape_concat_path(path)
        self.assertNotIn(
            "'\\''",
            escaped,
            "Shell-style quoting ('\\'\\''...) is invalid in ffmpeg concat format; "
            "use backslash escaping (\\\\') within single-quoted path strings.",
        )

    def test_apostrophe_escaped_with_backslash(self):
        """A single quote inside an ffmpeg concat path must be escaped as \\'.

        The concat file is written as: file '<escaped>\\n
        For a path containing ', the escaped form must be \\' so ffmpeg reads
        the entire path as one token.
        """
        path = Path("/downloads/New Year's Eve S01E01_seg0.ts")
        escaped = self.processor._escape_concat_path(path)
        self.assertIn(
            "\\'",
            escaped,
            "Apostrophe must be escaped as \\\\' for ffmpeg concat format.",
        )

    def test_apostrophe_full_concat_line_is_valid(self):
        """The full concat file line must not split the filename at the apostrophe.

        When ffmpeg concat parser sees file 'path\\'\\''rest', it reads 'path' as the
        filename and discards the rest, producing a "No such file or directory" error.
        """
        path = Path("/downloads/Britain's Got Talent S12E03_seg0.ts")
        escaped = self.processor._escape_concat_path(path)
        concat_line = f"file '{escaped}'"
        self.assertNotIn(
            "'\\''",
            concat_line,
            f"Malformed ffmpeg concat line: {concat_line!r}. "
            "ffmpeg parses only the partial path before the apostrophe.",
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
