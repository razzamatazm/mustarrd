"""
Regression test: Comskip sidecar files generated for the retry probe must be
cleaned up by detect_commercials.

Bug: when Comskip fails on a TS input, detect_commercials creates an
intermediate {stem}_comskip_input.mkv and re-runs Comskip on it. Comskip writes
its sidecar outputs (.txt/.log/.logo/.csv/.vdr/.xml depending on ini settings)
next to that probe file. The finally block removed only the .mkv probe itself,
so when the retry failed (or finished without an EDL) those sidecars were
orphaned in the download folder forever.

Fix: the finally block in detect_commercials also removes
{stem}_comskip_input.{txt,log,logo,csv,vdr,xml}. The .edl is excluded because
it can be the value returned to the caller (and is cleaned later by
_cleanup_comskip_outputs).
"""
import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_probe_sidecar_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor

SIDECAR_SUFFIXES = (".txt", ".log", ".logo", ".csv", ".vdr", ".xml")


def make_comskip_proc(returncode: int, on_finish=None):
    """Fake comskip subprocess with empty streams and the given exit code."""
    proc = MagicMock()
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_data(b"comskip output\n")
    stderr.feed_eof()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode

    async def _wait():
        if on_finish:
            on_finish()
        return returncode

    proc.wait = AsyncMock(side_effect=_wait)
    return proc


def make_prep_proc(returncode: int, probe_path: Path):
    """Fake prep ffmpeg subprocess that writes the probe file on success."""
    proc = MagicMock()

    async def _communicate():
        if returncode == 0:
            probe_path.write_bytes(b"\x00" * 256)
        return b"", b""

    proc.communicate = AsyncMock(side_effect=_communicate)
    proc.returncode = returncode
    return proc


class ComskipProbeSidecarCleanupTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    async def _run_detect(self, processor, ts_path, subprocesses):
        with patch.object(
            type(processor), "comskip_available",
            new_callable=PropertyMock, return_value=True,
        ), patch.object(
            type(processor), "ffmpeg_available",
            new_callable=PropertyMock, return_value=True,
        ), patch.object(
            processor, "_select_best_av_map_args",
            new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
        ), patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=subprocesses),
        ):
            return await processor.detect_commercials(str(ts_path))

    async def test_probe_sidecars_removed_when_comskip_retry_fails(self):
        """All probe sidecar files must be gone after a failed Comskip retry."""
        ts_path = Path(self.tmp) / "Show.ts"
        ts_path.write_bytes(b"\x47" * 188)
        probe_path = Path(self.tmp) / "Show_comskip_input.mkv"

        processor = PostProcessor()
        processor._comskip_path = "/usr/bin/comskip"
        processor._ffmpeg_path = "/usr/bin/ffmpeg"

        def write_sidecars():
            # Simulate Comskip writing its sidecar outputs for the probe input.
            for suffix in SIDECAR_SUFFIXES:
                probe_path.with_suffix(suffix).write_text("comskip sidecar")

        subprocesses = [
            make_comskip_proc(1),                            # first comskip run fails
            make_prep_proc(0, probe_path),                   # prep ffmpeg succeeds
            make_comskip_proc(1, on_finish=write_sidecars),  # retry fails, leaves sidecars
        ]

        with self.assertRaises(Exception):
            await self._run_detect(processor, ts_path, subprocesses)

        self.assertFalse(probe_path.exists(), "Probe .mkv must be removed.")
        for suffix in SIDECAR_SUFFIXES:
            sidecar = probe_path.with_suffix(suffix)
            self.assertFalse(
                sidecar.exists(),
                f"Probe sidecar {sidecar.name} must be removed after a failed "
                "Comskip retry; it currently orphans in the download folder.",
            )

    async def test_probe_edl_survives_cleanup_when_retry_succeeds(self):
        """The probe .edl returned to the caller must NOT be removed."""
        ts_path = Path(self.tmp) / "Show.ts"
        ts_path.write_bytes(b"\x47" * 188)
        probe_path = Path(self.tmp) / "Show_comskip_input.mkv"
        probe_edl = probe_path.with_suffix(".edl")

        processor = PostProcessor()
        processor._comskip_path = "/usr/bin/comskip"
        processor._ffmpeg_path = "/usr/bin/ffmpeg"

        def write_outputs():
            probe_edl.write_text("60.0 120.0 0\n")
            probe_path.with_suffix(".txt").write_text("comskip sidecar")

        subprocesses = [
            make_comskip_proc(1),                           # first comskip run fails
            make_prep_proc(0, probe_path),                  # prep ffmpeg succeeds
            make_comskip_proc(0, on_finish=write_outputs),  # retry succeeds with EDL
        ]

        edl_result = await self._run_detect(processor, ts_path, subprocesses)

        self.assertEqual(edl_result, str(probe_edl), "detect_commercials must return the probe EDL.")
        self.assertTrue(probe_edl.exists(), "The returned .edl must not be deleted by the cleanup.")
        self.assertFalse(probe_path.exists(), "Probe .mkv must be removed.")
        self.assertFalse(
            probe_path.with_suffix(".txt").exists(),
            "Probe .txt sidecar must be removed even when the retry succeeds.",
        )


if __name__ == "__main__":
    unittest.main()
