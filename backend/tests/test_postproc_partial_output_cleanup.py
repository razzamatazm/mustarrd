"""
Regression test: partially-written transcode/concat output files must be
removed by the post-processor itself on failure or cancellation.

Bugs:
- transcode() raised "ffmpeg failed" leaving the partial {stem}.mkv/.mp4 on
  disk, and a task cancel during the ffmpeg run also left the partial output.
- remove_commercials() only removed its partial output in the finally block
  when same_path_output was True (the _postproc_tmp case). With a different
  output extension (e.g. .ts -> .mkv) a failed or cancelled concat left the
  partial {stem}.mkv behind.

Fix: transcode() removes the partial output before raising and on
CancelledError; the remove_commercials finally block removes work_output_path
whenever the concat did not succeed.
"""
import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_partial_cleanup_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor
OutputFormat = PP_MODULE.OutputFormat
HardwareAccel = PP_MODULE.HardwareAccel


class PartialOutputCleanupTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ts_path = Path(self.tmp) / "Show.ts"
        self.ts_path.write_bytes(b"\x47" * 188)
        self.mkv_path = Path(self.tmp) / "Show.mkv"
        self.processor = PostProcessor()
        self.processor._ffmpeg_path = "/usr/bin/ffmpeg"

    def _patches(self, run_ffmpeg_side_effect):
        return [
            patch.object(
                type(self.processor), "ffmpeg_available",
                new_callable=PropertyMock, return_value=True,
            ),
            patch.object(
                self.processor, "_get_duration",
                new=AsyncMock(return_value=3600.0),
            ),
            patch.object(
                self.processor, "_select_best_av_map_args",
                new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
            ),
            patch.object(
                self.processor, "_run_ffmpeg_with_progress",
                side_effect=run_ffmpeg_side_effect,
            ),
        ]

    async def test_transcode_failure_removes_partial_output(self):
        """transcode() must delete the partial .mkv before raising on ffmpeg failure."""
        async def failing_run(cmd, *args, **kwargs):
            self.mkv_path.write_bytes(b"\x00" * 512)
            return 1, b"ffmpeg exploded"

        patches = self._patches(failing_run)
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(Exception):
                await self.processor.transcode(
                    str(self.ts_path),
                    OutputFormat.MKV,
                    hw_accel=HardwareAccel.CPU,
                    remux_only=False,
                )

        self.assertFalse(
            self.mkv_path.exists(),
            "Partial .mkv must be removed when transcode fails.",
        )
        self.assertTrue(self.ts_path.exists(), "Input file must not be touched.")

    async def test_transcode_cancel_removes_partial_output(self):
        """transcode() must delete the partial .mkv when the task is cancelled."""
        started = asyncio.Event()

        async def hanging_run(cmd, *args, **kwargs):
            self.mkv_path.write_bytes(b"\x00" * 512)
            started.set()
            await asyncio.sleep(3600)

        patches = self._patches(hanging_run)
        with patches[0], patches[1], patches[2], patches[3]:
            task = asyncio.create_task(self.processor.transcode(
                str(self.ts_path),
                OutputFormat.MKV,
                hw_accel=HardwareAccel.CPU,
                remux_only=False,
            ))
            await asyncio.wait_for(started.wait(), timeout=5.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(
            self.mkv_path.exists(),
            "Partial .mkv must be removed when transcode is cancelled.",
        )
        self.assertTrue(self.ts_path.exists(), "Input file must not be touched.")

    async def test_remove_commercials_concat_failure_removes_partial_mkv(self):
        """A failed concat with a different output extension must not orphan the partial .mkv."""
        edl_path = Path(self.tmp) / "Show.edl"
        edl_path.write_text("60.0 120.0 0\n")

        async def failing_concat(cmd, *args, **kwargs):
            self.mkv_path.write_bytes(b"\x00" * 512)
            return 1, b"concat failed"

        seg_proc = MagicMock()
        seg_proc.returncode = 0
        seg_proc.communicate = AsyncMock(return_value=(b"", b""))

        patches = self._patches(failing_concat)
        with patches[0], patches[1], patches[2], patches[3], patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=seg_proc),
        ):
            with self.assertRaises(Exception):
                await self.processor.remove_commercials(
                    str(self.ts_path),
                    str(edl_path),
                    output_format=OutputFormat.MKV,
                    hw_accel=HardwareAccel.CPU,
                    remux_only=False,
                )

        self.assertFalse(
            self.mkv_path.exists(),
            "Partial .mkv from a failed concat must be removed even when the "
            "output path differs from the input path (not the _postproc_tmp case).",
        )
        self.assertTrue(self.ts_path.exists(), "Input file must not be touched.")


if __name__ == "__main__":
    unittest.main()
