import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.epg import _build_like_pattern


class BuildLikePatternTests(unittest.TestCase):
    r"""_build_like_pattern() must treat %, _, and \ as literal characters."""

    def test_plain_text_unchanged(self):
        self.assertEqual(_build_like_pattern("hello"), "%hello%")

    def test_percent_escaped(self):
        # q=% must not become %%% (match-all); it becomes %\%%
        self.assertEqual(_build_like_pattern("%"), "%\\%%")

    def test_underscore_escaped(self):
        self.assertEqual(_build_like_pattern("_"), "%\\_%")

    def test_backslash_escaped_first(self):
        # A literal backslash in input must become \\ before % is escaped,
        # so that the added escape chars are not re-escaped.
        self.assertEqual(_build_like_pattern("\\"), "%\\\\%")

    def test_mixed_wildcards(self):
        self.assertEqual(_build_like_pattern("a%b_c"), "%a\\%b\\_c%")

    def test_backslash_before_percent(self):
        # Input "\%" must become "\\\\\\%" — not "\\%%" (double-escaped percent)
        self.assertEqual(_build_like_pattern("\\%"), "%\\\\\\%%")

    def test_lowercased_before_pattern(self):
        # Caller lowercases q before passing; verify case is preserved after
        self.assertEqual(_build_like_pattern("ABC"), "%ABC%")

    def test_percent_only_query_not_match_all(self):
        # Before fix: "%%" LIKE pattern would match every row.
        # After fix: the pattern must contain an escaped percent.
        pattern = _build_like_pattern("%")
        self.assertIn("\\%", pattern)
        self.assertNotEqual(pattern, "%%%")

    def test_twenty_underscores_not_match_all(self):
        pattern = _build_like_pattern("_" * 20)
        self.assertNotIn("%_%", pattern)
        self.assertEqual(pattern.count("\\_"), 20)
