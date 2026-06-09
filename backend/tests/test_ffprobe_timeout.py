"""
Regression test: ffprobe invocations must time out instead of hanging forever.

Bug: _get_duration and _select_best_av_map_args awaited process.communicate()
with no timeout. A corrupt or truncated input file can make ffprobe block
indefinitely, which stalls the whole post-processing pipeline for that download
forever (the post-processing slot is never released).

Fix: both helpers wrap communicate() in asyncio.wait_for with
FFPROBE_TIMEOUT_SECONDS (60s default), kill the ffprobe process on timeout, and
fall back cleanly (duration 0 / primary stream map args).
"""
import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_ffprobe_timeout_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor


class HangingProcess:
    """Fake subprocess whose communicate() never returns until terminated."""

    def __init__(self):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False

    async def communicate(self):
        await asyncio.sleep(3600)

    def terminate(self):
        self.terminate_called = True
        self.returncode = -15

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FfprobeTimeoutTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.processor = PostProcessor()
        # Short timeout so the test runs fast; production default is 60s.
        self.processor.FFPROBE_TIMEOUT_SECONDS = 0.1

    async def test_get_duration_times_out_and_kills_ffprobe(self):
        """_get_duration must return 0 and kill ffprobe when it hangs."""
        proc = HangingProcess()
        with patch.object(self.processor, "_resolve_ffprobe_path",
                          return_value="/usr/bin/ffprobe"), \
             patch("asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=proc)):
            try:
                duration = await asyncio.wait_for(
                    self.processor._get_duration("/tmp/corrupt.ts"), timeout=2.0
                )
            except asyncio.TimeoutError:
                self.fail(
                    "_get_duration hung on a stalled ffprobe; it must enforce "
                    "its own timeout and return a fallback duration."
                )

        self.assertEqual(duration, 0, "Timed-out probe must report duration 0.")
        self.assertTrue(
            proc.terminate_called or proc.kill_called,
            "The hung ffprobe process must be terminated on timeout.",
        )

    async def test_select_best_av_map_args_times_out_and_kills_ffprobe(self):
        """_select_best_av_map_args must fall back to primary streams on hang."""
        proc = HangingProcess()
        with patch.object(self.processor, "_resolve_ffprobe_path",
                          return_value="/usr/bin/ffprobe"), \
             patch("asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=proc)):
            try:
                map_args = await asyncio.wait_for(
                    self.processor._select_best_av_map_args("/tmp/corrupt.ts"),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                self.fail(
                    "_select_best_av_map_args hung on a stalled ffprobe; it must "
                    "enforce its own timeout and fall back to primary streams."
                )

        self.assertEqual(
            map_args,
            self.processor._primary_av_map_args(),
            "Timed-out stream probe must fall back to primary stream mapping.",
        )
        self.assertTrue(
            proc.terminate_called or proc.kill_called,
            "The hung ffprobe process must be terminated on timeout.",
        )

    async def test_get_duration_kills_ffprobe_on_task_cancel(self):
        """Cancelling the surrounding task must not leave ffprobe running."""
        proc = HangingProcess()
        self.processor.FFPROBE_TIMEOUT_SECONDS = 3600
        with patch.object(self.processor, "_resolve_ffprobe_path",
                          return_value="/usr/bin/ffprobe"), \
             patch("asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=proc)):
            task = asyncio.create_task(self.processor._get_duration("/tmp/corrupt.ts"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(
            proc.terminate_called or proc.kill_called,
            "The ffprobe process must be terminated when the task is cancelled.",
        )


if __name__ == "__main__":
    unittest.main()
