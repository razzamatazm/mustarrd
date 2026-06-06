"""
Regression tests for post_processor EDL parsing and commercial removal edge cases.

Bug 1: _invert_segments produces overlapping keep segments when an EDL entry has
       start > end (inverted timestamps, possible from Comskip on IPTV streams
       with non-monotonic PTS). Result: duplicated video content, no error raised.

Bug 2: Comskip retry path (_comskip_input temp probe) leaves auxiliary files
       (.txt, .log, .logo, .csv, .vdr, .xml) on disk after successful second run.
       _cleanup_comskip_outputs only removes the EDL and files named after the
       ORIGINAL input, not the temp probe siblings.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_edl_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class InvertedEdlSegmentTests(unittest.TestCase):
    """Bug 1: _parse_edl + _invert_segments with inverted timestamps (start > end)."""

    def setUp(self):
        self.processor = PostProcessor()

    def _write_edl(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".edl", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_parse_edl_rejects_inverted_entry(self):
        """_parse_edl must skip entries where start > end."""
        edl = self._write_edl("60.000000 10.000000 0\n")
        segments = self.processor._parse_edl(edl)
        self.assertEqual(len(segments), 0, "Inverted EDL entry (start > end) must be skipped")

    def test_invert_segments_inverted_entry_no_overlap(self):
        """_invert_segments must skip an inverted entry (start > end); no overlapping keeps.

        Input: commercial segment (60, 10) is inverted. After the fix, _invert_segments
        skips it, so no duplicate content window is produced.
        """
        # Commercial claimed to be from t=60 to t=10 (inverted)
        segments = [(60.0, 10.0, 0)]
        duration = 3600.0
        keep = self.processor._invert_segments(segments, duration)

        for i, (s1, e1) in enumerate(keep):
            for j, (s2, e2) in enumerate(keep):
                if i >= j:
                    continue
                overlap = min(e1, e2) - max(s1, s2)
                self.assertLessEqual(
                    overlap, 0.0,
                    f"Keep segments [{i}]=({s1},{e1}) and [{j}]=({s2},{e2}) overlap "
                    f"by {overlap}s; inverted EDL entry must not produce duplicate content"
                )

    def test_invert_segments_multiple_inverted_entries_no_overlap(self):
        """Multiple inverted EDL entries must be skipped; no overlapping keeps produced."""
        segments = [(60.0, 10.0, 0), (90.0, 80.0, 0)]
        duration = 3600.0
        keep = self.processor._invert_segments(segments, duration)

        overlaps_found = 0
        for i, (s1, e1) in enumerate(keep):
            for j, (s2, e2) in enumerate(keep):
                if i >= j:
                    continue
                overlap = min(e1, e2) - max(s1, s2)
                if overlap > 0:
                    overlaps_found += 1

        self.assertEqual(
            overlaps_found, 0,
            "Inverted EDL entries must be skipped; keep segments must not overlap"
        )


class ComskipTempProbeOrphanTests(unittest.TestCase):
    """Bug 2: _cleanup_comskip_outputs leaves temp probe auxiliary files on disk."""

    def setUp(self):
        self.processor = PostProcessor()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def _make_file(self, name: str) -> Path:
        p = Path(self.tmp) / name
        p.write_text("dummy")
        return p

    def test_cleanup_removes_original_input_siblings(self):
        """Sanity check: cleanup does remove artifacts next to the original input."""
        original = self._make_file("Show.ts")
        edl = self._make_file("Show.edl")
        txt = self._make_file("Show.txt")

        self.processor._cleanup_comskip_outputs(str(original), str(edl))

        self.assertFalse(edl.exists(), "Show.edl should be removed")
        self.assertFalse(txt.exists(), "Show.txt should be removed")

    def test_cleanup_removes_temp_probe_siblings(self):
        """_cleanup_comskip_outputs must remove auxiliary files next to the temp probe EDL.

        When Comskip fails on the original .ts and succeeds on the _comskip_input.mkv
        probe, _cleanup_comskip_outputs is called with:
          input_path = "Show.ts"
          edl_path   = "Show_comskip_input.edl"

        After the fix it removes Show_comskip_input.{edl,txt,log,logo,...} in addition
        to Show.{txt,log,...}, so no orphan files are left on disk.
        """
        original = self._make_file("Show.ts")
        # Files Comskip writes next to the temp probe file
        probe_edl = self._make_file("Show_comskip_input.edl")
        probe_txt = self._make_file("Show_comskip_input.txt")
        probe_log = self._make_file("Show_comskip_input.log")
        probe_logo = self._make_file("Show_comskip_input.logo")

        self.processor._cleanup_comskip_outputs(str(original), str(probe_edl))

        self.assertFalse(probe_edl.exists(), "Show_comskip_input.edl should be removed")
        self.assertFalse(probe_txt.exists(), "Show_comskip_input.txt should be removed")
        self.assertFalse(probe_log.exists(), "Show_comskip_input.log should be removed")
        self.assertFalse(probe_logo.exists(), "Show_comskip_input.logo should be removed")


if __name__ == "__main__":
    unittest.main()
