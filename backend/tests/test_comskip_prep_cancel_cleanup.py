"""
Regression test: cancelling a download during the Comskip prep FFmpeg step must
kill the FFmpeg subprocess and remove the partial _comskip_input.mkv file.

Bug: in detect_commercials, the prep FFmpeg (which normalizes a failing TS into
an intermediate MKV for the Comskip retry) was awaited via communicate() with no
CancelledError handling, and the temp probe file cleanup lived in a try/finally
that only started AFTER the prep block. Cancelling the post-processing task
while prep was running left the FFmpeg process alive (still writing) and the
partial {stem}_comskip_input.mkv orphaned on disk.

Fix: prep FFmpeg runs through a helper that terminates the subprocess on
CancelledError, and the try/finally now wraps the whole retry block so the
probe file is removed on every exit path.
"""
import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_prep_cancel_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor


def make_failed_comskip_proc():
    """Fake comskip process that exits non-zero (triggers the TS retry path)."""
    proc = MagicMock()
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_data(b"comskip: could not parse TS\n")
    stderr.feed_eof()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = 1
    proc.wait = AsyncMock(return_value=1)
    return proc


class HangingPrepProcess:
    """Fake prep FFmpeg that writes a partial probe file then blocks forever."""

    def __init__(self, probe_path: Path):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.started = asyncio.Event()
        self._probe_path = probe_path

    async def communicate(self):
        self._probe_path.write_bytes(b"\x00" * 256)  # partial ffmpeg output
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


class ComskipPrepCancelCleanupTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    async def test_cancel_during_prep_kills_ffmpeg_and_removes_probe_file(self):
        ts_path = Path(self.tmp) / "Show.ts"
        ts_path.write_bytes(b"\x47" * 188)
        probe_path = Path(self.tmp) / "Show_comskip_input.mkv"

        processor = PostProcessor()
        processor._comskip_path = "/usr/bin/comskip"
        processor._ffmpeg_path = "/usr/bin/ffmpeg"

        prep_proc = HangingPrepProcess(probe_path)
        subprocesses = [make_failed_comskip_proc(), prep_proc]

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
            task = asyncio.create_task(processor.detect_commercials(str(ts_path)))
            await asyncio.wait_for(prep_proc.started.wait(), timeout=5.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(
            prep_proc.terminate_called or prep_proc.kill_called,
            "The prep FFmpeg subprocess must be terminated when the post-process "
            "task is cancelled; otherwise it keeps running and writing to disk.",
        )
        self.assertFalse(
            probe_path.exists(),
            "The partial _comskip_input.mkv must be removed when the task is "
            "cancelled during the Comskip prep step.",
        )


if __name__ == "__main__":
    unittest.main()
