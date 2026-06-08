"""
Regression test: _invert_segments silently includes commercial content when an EDL
contains contained/nested segments.

Bug: When commercial segment B is contained within segment A (A.start < B.start and
B.end < A.end), _invert_segments processes them in sorted order. After A sets
current_pos = A.end, B sets current_pos = B.end, which is LESS than A.end.
The next keep segment then starts at B.end instead of A.end, pulling in A.end - B.end
seconds of commercial content.

Example:
  Commercial A: 10.0 to 80.0
  Commercial B: 30.0 to 50.0 (contained inside A)

  After sorting by start: A then B.
  After A: current_pos = 80.0
  After B: start=30 < current_pos=80, no keep added. current_pos = 50.0  (regression)
  After loop: keep (50.0, 120.0)

  Result:  [(0.0, 10.0), (50.0, 120.0)]  -- 30 seconds of commercial included
  Expected: [(0.0, 10.0), (80.0, 120.0)]
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_contained_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class ContainedSegmentTests(unittest.TestCase):
    def setUp(self):
        self.processor = PostProcessor()

    def _write_edl(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".edl", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_invert_segments_contained_segment_does_not_regress_current_pos(self):
        """current_pos must not regress when a segment is contained within a prior one.

        If current_pos regresses, content from B.end to A.end is incorrectly included
        in the output even though it is inside commercial A.
        """
        # (30, 50) is contained within (10, 80)
        segments = [(10.0, 80.0, 0), (30.0, 50.0, 0)]
        duration = 120.0
        keep = self.processor._invert_segments(segments, duration)

        for start, end in keep:
            self.assertGreaterEqual(
                start, 0.0,
                "Keep segment must not start before 0"
            )
            # No keep segment must start inside the commercial window (10, 80)
            self.assertFalse(
                10.0 < start < 80.0,
                f"Keep segment start={start} falls inside commercial (10, 80); "
                "commercial content included in output"
            )

    def test_invert_segments_contained_segment_tail_starts_at_outer_end(self):
        """After removing a commercial with a contained segment, content resumes at
        the outer segment's end (80.0), not the inner segment's end (50.0).
        """
        segments = [(10.0, 80.0, 0), (30.0, 50.0, 0)]
        duration = 120.0
        keep = self.processor._invert_segments(segments, duration)

        # The tail keep segment should start at 80.0 (outer end), not 50.0 (inner end)
        tail_starts = [s for s, e in keep if s > 10.0]
        self.assertTrue(
            all(s >= 80.0 for s in tail_starts),
            f"Tail keep segment(s) start before outer commercial end (80.0): {keep}. "
            "Expected first tail start at 80.0, not 50.0"
        )

    def test_invert_segments_fully_contained_segment_no_content_gap(self):
        """A segment fully contained within another must not create a content window
        inside the commercial.
        """
        # (0, 100) contains (20, 40)
        segments = [(0.0, 100.0, 0), (20.0, 40.0, 0)]
        duration = 200.0
        keep = self.processor._invert_segments(segments, duration)

        for s, e in keep:
            overlap_start = max(s, 0.0)
            overlap_end = min(e, 100.0)
            self.assertLessEqual(
                overlap_end - overlap_start, 0.0,
                f"Keep segment ({s}, {e}) overlaps with commercial (0, 100). "
                "Contained segment caused content gap regression"
            )

    def test_parse_edl_contained_segments_both_accepted(self):
        """_parse_edl accepts both segments when both have end > start.

        The outer segment (10, 80) and contained (30, 50) both have end > start, so
        both are parsed. This confirms the regression happens in _invert_segments,
        not _parse_edl.
        """
        edl = self._write_edl("10.000000 80.000000 0\n30.000000 50.000000 0\n")
        segments = self.processor._parse_edl(edl)
        self.assertEqual(len(segments), 2, "Both valid segments must be parsed")

    def test_invert_segments_adjacent_segments_not_affected(self):
        """Adjacent (non-overlapping, non-contained) segments must still work correctly."""
        segments = [(10.0, 50.0, 0), (50.0, 80.0, 0)]
        duration = 120.0
        keep = self.processor._invert_segments(segments, duration)
        # Content: [0,10] then [80,120]
        self.assertEqual(len(keep), 2, f"Expected 2 keep segments, got: {keep}")
        self.assertAlmostEqual(keep[0][0], 0.0)
        self.assertAlmostEqual(keep[0][1], 10.0)
        self.assertAlmostEqual(keep[1][0], 80.0)
        self.assertAlmostEqual(keep[1][1], 120.0)


if __name__ == "__main__":
    unittest.main()
