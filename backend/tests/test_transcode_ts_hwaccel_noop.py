"""
Regression test: transcode() with TS output and non-CPU hw_accel used to truncate the
recording to 0 bytes.

Root cause: the early-exit guard checked `hw_accel == CPU`, so VAAPI/NVIDIA/AMD/Apple
Silicon fell through. For TS output ffmpeg always uses -c copy (no HW encoder) and
writes to the same path as its input, which truncates the file before reading starts.

Fix: add a dedicated TS-to-TS guard before the CPU check. TS output is always a copy
regardless of hw_accel, so if the input is already .ts, return it unchanged.
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
SPEC = importlib.util.spec_from_file_location("post_processor_under_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

HardwareAccel = POST_PROCESSOR_MODULE.HardwareAccel
OutputFormat = POST_PROCESSOR_MODULE.OutputFormat
PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class TranscodeTsHwAccelNoopTests(unittest.IsolatedAsyncioTestCase):
    """transcode() must return the input path unchanged for TS-to-TS, regardless of hw_accel."""

    async def _assert_ts_to_ts_skips_ffmpeg(self, hw_accel: HardwareAccel):
        processor = PostProcessor()
        processor._ffmpeg_path = "ffmpeg"
        file_content = b"\x47" * 4096

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(file_content)
            tmp_path = f.name

        try:
            with patch.object(
                processor, "_run_ffmpeg_with_progress", new_callable=AsyncMock
            ) as mock_ffmpeg:
                result = await processor.transcode(
                    tmp_path, OutputFormat.TS, hw_accel=hw_accel
                )

            mock_ffmpeg.assert_not_called()
            self.assertEqual(result, tmp_path, "Must return original path unchanged.")
            self.assertEqual(
                Path(tmp_path).stat().st_size,
                len(file_content),
                "Input file must not be truncated.",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    async def test_ts_to_ts_cpu_skips_ffmpeg(self):
        """Baseline: CPU hw_accel already worked before the fix."""
        await self._assert_ts_to_ts_skips_ffmpeg(HardwareAccel.CPU)

    async def test_ts_to_ts_vaapi_skips_ffmpeg(self):
        """VAAPI must not invoke ffmpeg for TS-to-TS (was data-loss bug)."""
        await self._assert_ts_to_ts_skips_ffmpeg(HardwareAccel.VAAPI)

    async def test_ts_to_ts_nvidia_skips_ffmpeg(self):
        """NVIDIA must not invoke ffmpeg for TS-to-TS."""
        await self._assert_ts_to_ts_skips_ffmpeg(HardwareAccel.NVIDIA)

    async def test_ts_to_ts_amd_skips_ffmpeg(self):
        """AMD must not invoke ffmpeg for TS-to-TS."""
        await self._assert_ts_to_ts_skips_ffmpeg(HardwareAccel.AMD)

    async def test_ts_to_mp4_vaapi_does_call_ffmpeg(self):
        """TS-to-MP4 with VAAPI must still invoke ffmpeg (no regression)."""
        processor = PostProcessor()
        processor._ffmpeg_path = "ffmpeg"

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(b"\x47" * 4096)
            tmp_path = f.name

        output_path = Path(tmp_path).with_suffix(".mp4")

        try:
            with (
                patch.object(
                    type(processor), "ffmpeg_available",
                    new_callable=lambda: property(lambda self: True),
                ),
                patch.object(
                    processor,
                    "_select_best_av_map_args",
                    AsyncMock(return_value=["-map", "0"]),
                ),
                patch.object(
                    processor,
                    "_run_ffmpeg_with_progress",
                    new_callable=AsyncMock,
                    return_value=(0, b""),
                ) as mock_ffmpeg,
                patch.object(processor, "_get_duration", AsyncMock(return_value=60.0)),
                patch.object(processor, "_write_ffmpeg_log", return_value=None),
            ):
                await processor.transcode(
                    tmp_path, OutputFormat.MP4, hw_accel=HardwareAccel.VAAPI
                )

            mock_ffmpeg.assert_called()
        except Exception:
            pass
        finally:
            for path in (tmp_path, str(output_path)):
                if os.path.exists(path):
                    os.unlink(path)

        self.assertTrue(
            mock_ffmpeg.called,
            "TS-to-MP4 with VAAPI must call ffmpeg (no regression from TS-to-TS fix).",
        )


if __name__ == "__main__":
    unittest.main()
