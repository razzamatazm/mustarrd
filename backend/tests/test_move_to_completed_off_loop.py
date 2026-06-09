"""
Regression tests: _move_to_completed must not block the event loop.

A cross-filesystem shutil.move (e.g. Docker volume -> NAS) of a multi-GB
recording is a synchronous copy that can take minutes. Every call site in
download_manager awaits it from the event loop, so a blocking move froze
all concurrent downloads, WebSocket progress updates, and API requests.

Fix: call sites go through _move_to_completed_async, which runs the move
in a worker thread via asyncio.to_thread.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


class MoveToCompletedOffLoopTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.manager = DownloadManager()
        self.src_dir = tempfile.mkdtemp(prefix="dl_src_")
        self.dst_dir = tempfile.mkdtemp(prefix="dl_dst_")
        self.src_file = os.path.join(self.src_dir, "show.ts")
        with open(self.src_file, "wb") as fh:
            fh.write(b"payload")

    def tearDown(self):
        shutil.rmtree(self.src_dir, ignore_errors=True)
        shutil.rmtree(self.dst_dir, ignore_errors=True)

    async def test_move_runs_off_the_event_loop_thread(self):
        """shutil.move must execute in a worker thread, not the loop thread."""
        loop_thread = threading.current_thread()
        move_thread = []
        original_move = shutil.move

        def recording_move(src, dst):
            move_thread.append(threading.current_thread())
            return original_move(src, dst)

        with patch("services.download_manager.shutil.move", recording_move):
            dest = await self.manager._move_to_completed_async(
                self.src_file, self.dst_dir, self.src_dir
            )

        self.assertEqual(len(move_thread), 1, "shutil.move must be called exactly once")
        self.assertIsNot(
            move_thread[0],
            loop_thread,
            "shutil.move ran on the event-loop thread; a slow cross-filesystem "
            "move would freeze every download and WebSocket update.",
        )
        self.assertTrue(os.path.isfile(dest), "file must arrive in the completed folder")
        self.assertFalse(os.path.exists(self.src_file), "source must be moved away")

    async def test_event_loop_stays_responsive_during_slow_move(self):
        """Other coroutines must keep running while a slow move is in flight."""
        original_move = shutil.move

        def slow_move(src, dst):
            time.sleep(0.3)  # simulate a slow cross-filesystem copy
            return original_move(src, dst)

        ticks = []

        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        try:
            with patch("services.download_manager.shutil.move", slow_move):
                await self.manager._move_to_completed_async(
                    self.src_file, self.dst_dir, self.src_dir
                )
        finally:
            beat.cancel()

        self.assertGreater(
            len(ticks),
            5,
            "Event loop made almost no progress during the move; the move is "
            "blocking the loop instead of running in a worker thread.",
        )

    async def test_oserror_from_move_propagates_through_async_wrapper(self):
        """ENOSPC raised inside the worker thread must surface to the caller."""
        def enospc_move(src, dst):
            raise OSError(28, "No space left on device")

        with patch("services.download_manager.shutil.move", enospc_move):
            with self.assertRaises(OSError):
                await self.manager._move_to_completed_async(
                    self.src_file, self.dst_dir, self.src_dir
                )

        self.assertTrue(
            os.path.exists(self.src_file),
            "source file must survive a failed move",
        )


if __name__ == "__main__":
    unittest.main()
