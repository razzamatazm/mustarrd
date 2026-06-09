"""
Regression test: cancelling a download during segment extraction in
remove_commercials must kill the FFmpeg subprocess.

Bug: the per-segment FFmpeg copy in remove_commercials was awaited via
communicate() with no CancelledError handling. Cancelling the post-processing
task left the FFmpeg process running and writing the _segN.ts file, while the
finally block simultaneously tried to delete it — the process survived the
cancel and could re-create the file after cleanup.

Fix: communicate() is wrapped so CancelledError terminates the subprocess
before the cancellation propagates (same pattern as _run_ffmpeg_with_progress).
"""
import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_seg_cancel_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor
OutputFormat = PP_MODULE.OutputFormat
HardwareAccel = PP_MODULE.HardwareAccel


class HangingSegmentProcess:
    """Fake segment-extraction FFmpeg whose communicate() blocks until killed."""

    def __init__(self):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.started = asyncio.Event()

    async def communicate(self):
        self.started.set()
        await asyncio.sleep(3600)

    def terminate(self):
        self.terminate_called = True
        self.returncode = -15

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class SegmentExtractCancelKillTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    async def test_cancel_during_segment_extraction_kills_ffmpeg(self):
        ts_path = Path(self.tmp) / "Show.ts"
        ts_path.write_bytes(b"\x47" * 188)
        edl_path = Path(self.tmp) / "Show.edl"
        edl_path.write_text("60.0 120.0 0\n")

        processor = PostProcessor()
        processor._ffmpeg_path = "/usr/bin/ffmpeg"

        seg_proc = HangingSegmentProcess()

        with patch.object(
            type(processor), "ffmpeg_available",
            new_callable=PropertyMock, return_value=True,
        ), patch.object(
            processor, "_get_duration",
            new=AsyncMock(return_value=3600.0),
        ), patch.object(
            processor, "_select_best_av_map_args",
            new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
        ), patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=seg_proc),
        ):
            task = asyncio.create_task(processor.remove_commercials(
                str(ts_path),
                str(edl_path),
                output_format=OutputFormat.MKV,
                hw_accel=HardwareAccel.CPU,
            ))
            await asyncio.wait_for(seg_proc.started.wait(), timeout=5.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(
            seg_proc.terminate_called or seg_proc.kill_called,
            "The segment-extraction FFmpeg subprocess must be terminated when "
            "the post-processing task is cancelled; otherwise it keeps running "
            "and writing segment files after cleanup.",
        )


if __name__ == "__main__":
    unittest.main()
