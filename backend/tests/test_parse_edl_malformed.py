import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_under_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


def _write_edl(tmp_dir, lines):
    path = Path(tmp_dir) / "test.edl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


class ParseEdlMalformedTests(unittest.TestCase):
    def setUp(self):
        self.processor = PostProcessor()

    def test_valid_edl_parses_correctly(self):
        """Baseline: a well-formed EDL file returns the expected segments."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_edl(tmp, ["0.000000 30.000000 0", "60.000000 90.000000 3"])
            segments = self.processor._parse_edl(path)
        self.assertEqual(segments, [(0.0, 30.0, 0), (60.0, 90.0, 3)])

    def test_na_timing_field_skipped_not_crash(self):
        """
        BUG: ComSkip can emit N/A for timing values on short or corrupt input.
        Before the fix, float("N/A") raised ValueError and crashed _parse_edl,
        marking the download FAILED with a cryptic Python traceback.

        After the fix, malformed lines are skipped and processing continues.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_edl(tmp, ["N/A 30.000000 3", "60.000000 90.000000 0"])
            # Must not raise ValueError
            segments = self.processor._parse_edl(path)
        # The bad line is skipped; the valid line is returned
        self.assertEqual(segments, [(60.0, 90.0, 0)])

    def test_all_malformed_lines_returns_empty(self):
        """All-malformed EDL yields empty list, not a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_edl(tmp, ["N/A N/A 0", "abc 30.0 3", "0.0 N/A 0"])
            segments = self.processor._parse_edl(path)
        self.assertEqual(segments, [])

    def test_non_numeric_seg_type_skipped(self):
        """Non-integer segment type is skipped without crash."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_edl(tmp, ["0.0 30.0 cut", "60.0 90.0 3"])
            segments = self.processor._parse_edl(path)
        self.assertEqual(segments, [(60.0, 90.0, 3)])

    def test_blank_and_short_lines_ignored(self):
        """Lines with fewer than 3 fields are silently skipped (pre-existing behavior)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_edl(tmp, ["", "0.0 30.0", "60.0 90.0 0"])
            segments = self.processor._parse_edl(path)
        self.assertEqual(segments, [(60.0, 90.0, 0)])


if __name__ == "__main__":
    unittest.main()
