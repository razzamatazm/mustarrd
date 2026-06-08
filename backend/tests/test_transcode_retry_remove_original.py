"""
Regression test: transcode() with remux_only=True that falls back to full encode on
remux failure does not honour remove_original=True.

Root cause: when the remux fails and the full-encode retry succeeds, transcode() returns
early (the "if returncode == 0: return str(output_path)" branch inside the remux-retry
block) before reaching the remove_original deletion at the end of the function.

Effect: users with "delete original after transcode" enabled who hit a remux failure
(common when the TS has non-standard audio requiring re-encode) end up with both the
original .ts AND the transcoded .mkv/.mp4 on disk. Double disk usage. Recovery on
restart may re-trigger post-processing if the .ts is found in the download folder.

Reproduction:
  1. Settings: transcode_format=mkv, remux_only=True, delete_original_after_transcode=True.
  2. Provide a TS file whose audio causes ffmpeg remux to fail (e.g., AC3 with unusual
     PTS gaps that copy-mode rejects) so the retry with full encode is triggered.
  3. After post-processing completes successfully, check the download folder: the
     original .ts file is still present alongside the new .mkv.

Expected: original .ts deleted after retry succeeds when remove_original=True.
Actual:   original .ts left on disk; only the .mkv is moved to completed/.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MODULE_PATH = BACKEND_ROOT / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_retry_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

HardwareAccel = POST_PROCESSOR_MODULE.HardwareAccel
OutputFormat = POST_PROCESSOR_MODULE.OutputFormat
PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class TranscodeRetryRemoveOriginalTests(unittest.IsolatedAsyncioTestCase):
    """transcode() retry-success path must honour remove_original=True."""

    async def test_remux_fail_encode_retry_success_removes_original(self):
        """When remux fails and full-encode retry succeeds, original .ts must be deleted.

        transcode(remux_only=True) first attempts stream copy (remux). On failure it
        retries with full re-encode. The retry success path hits an early return before
        the remove_original deletion block, leaving the .ts on disk.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            input_ts = Path(tmpdir) / "Show.ts"
            input_ts.write_bytes(b"fake ts content")
            output_mkv = Path(tmpdir) / "Show.mkv"

            processor = PostProcessor()
            processor._ffmpeg_path = "/usr/bin/ffmpeg"

            call_count = 0

            async def mock_run_ffmpeg(cmd, duration, callback=None, env=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # First call: remux attempt fails
                    return 1, b"remux failed: invalid data interleaving"
                # Second call: full-encode retry; write output to simulate success
                output_mkv.write_bytes(b"encoded mkv content")
                return 0, b""

            with (
                patch.object(
                    processor,
                    "_run_ffmpeg_with_progress",
                    side_effect=mock_run_ffmpeg,
                ),
                patch.object(
                    processor, "_get_duration", new=AsyncMock(return_value=3600.0)
                ),
                patch.object(
                    processor,
                    "_select_best_av_map_args",
                    new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
                ),
                patch.object(
                    processor, "_resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
                ),
            ):
                result = await processor.transcode(
                    str(input_ts),
                    OutputFormat.MKV,
                    hw_accel=HardwareAccel.CPU,
                    remux_only=True,
                    remove_original=True,
                )

            self.assertEqual(call_count, 2, "Expected remux attempt then full-encode retry.")
            self.assertEqual(result, str(output_mkv), "Must return transcoded output path.")
            self.assertFalse(
                input_ts.exists(),
                "Original .ts must be deleted when remove_original=True and retry succeeds. "
                "Bug: early return in the retry block bypasses the remove_original deletion.",
            )

    async def test_remux_success_removes_original(self):
        """Baseline: when remux succeeds on first try, original is deleted (no regression)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_ts = Path(tmpdir) / "Show.ts"
            input_ts.write_bytes(b"fake ts content")
            output_mkv = Path(tmpdir) / "Show.mkv"

            processor = PostProcessor()
            processor._ffmpeg_path = "/usr/bin/ffmpeg"

            async def mock_run_ffmpeg(cmd, duration, callback=None, env=None):
                output_mkv.write_bytes(b"remuxed mkv content")
                return 0, b""

            with (
                patch.object(
                    processor,
                    "_run_ffmpeg_with_progress",
                    side_effect=mock_run_ffmpeg,
                ),
                patch.object(
                    processor, "_get_duration", new=AsyncMock(return_value=3600.0)
                ),
                patch.object(
                    processor,
                    "_select_best_av_map_args",
                    new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
                ),
                patch.object(
                    processor, "_resolve_ffmpeg_path", return_value="/usr/bin/ffmpeg"
                ),
            ):
                result = await processor.transcode(
                    str(input_ts),
                    OutputFormat.MKV,
                    hw_accel=HardwareAccel.CPU,
                    remux_only=True,
                    remove_original=True,
                )

            self.assertFalse(
                input_ts.exists(),
                "Original .ts must be deleted when remux succeeds with remove_original=True.",
            )


if __name__ == "__main__":
    unittest.main()
