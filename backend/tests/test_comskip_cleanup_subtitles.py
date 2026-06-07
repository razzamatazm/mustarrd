"""
Regression test for _cleanup_comskip_outputs deleting user subtitle files.

Bug: .srt, .vtt, .ass, and .xml files were included in the ComSkip cleanup
candidate list. ComSkip never generates these formats; users place subtitle
files alongside recordings. All four were silently deleted after any ComSkip
post-processing run, with no way to recover them.

Fix: only .edl, .txt, .log, .logo, .csv, .vdr are ComSkip outputs. The other
extensions are removed from the cleanup list.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_subtitle_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class ComskipCleanupSubtitleProtectionTests(unittest.TestCase):
    def setUp(self):
        self.processor = PostProcessor()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def _make_file(self, name: str) -> Path:
        p = Path(self.tmp) / name
        p.write_text("dummy")
        return p

    def test_subtitle_files_survive_cleanup(self):
        """User subtitle files next to the recording must not be deleted."""
        original = self._make_file("Show.S01E01.ts")
        edl = self._make_file("Show.S01E01.edl")
        srt = self._make_file("Show.S01E01.srt")
        vtt = self._make_file("Show.S01E01.vtt")
        ass = self._make_file("Show.S01E01.ass")
        xml = self._make_file("Show.S01E01.xml")

        self.processor._cleanup_comskip_outputs(str(original), str(edl))

        self.assertFalse(edl.exists(), ".edl should be cleaned up (ComSkip output)")
        self.assertTrue(srt.exists(), ".srt must not be deleted (user subtitle)")
        self.assertTrue(vtt.exists(), ".vtt must not be deleted (user subtitle)")
        self.assertTrue(ass.exists(), ".ass must not be deleted (user subtitle)")
        self.assertTrue(xml.exists(), ".xml must not be deleted (user metadata)")

    def test_comskip_outputs_still_cleaned(self):
        """Real ComSkip output files are still removed after the fix."""
        original = self._make_file("Show.ts")
        edl = self._make_file("Show.edl")
        txt = self._make_file("Show.txt")
        log = self._make_file("Show.log")
        logo = self._make_file("Show.logo")
        csv = self._make_file("Show.csv")
        vdr = self._make_file("Show.vdr")

        self.processor._cleanup_comskip_outputs(str(original), str(edl))

        for f, name in [(edl, ".edl"), (txt, ".txt"), (log, ".log"),
                        (logo, ".logo"), (csv, ".csv"), (vdr, ".vdr")]:
            self.assertFalse(f.exists(), f"{name} should be cleaned up")

    def test_probe_sibling_subtitles_survive_cleanup(self):
        """Subtitle files alongside the _comskip_input probe must not be deleted."""
        original = self._make_file("Show.ts")
        probe_edl = self._make_file("Show_comskip_input.edl")
        probe_srt = self._make_file("Show_comskip_input.srt")
        probe_xml = self._make_file("Show_comskip_input.xml")

        self.processor._cleanup_comskip_outputs(str(original), str(probe_edl))

        self.assertFalse(probe_edl.exists(), "probe .edl should be cleaned up")
        self.assertTrue(probe_srt.exists(), "probe .srt must not be deleted")
        self.assertTrue(probe_xml.exists(), "probe .xml must not be deleted")


if __name__ == "__main__":
    unittest.main()
