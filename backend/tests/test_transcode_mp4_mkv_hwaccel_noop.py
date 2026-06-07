"""
Regression test: transcode() with MP4 or MKV output and non-CPU hw_accel used to
truncate the recording to 0 bytes.

Root cause: the same-format early-exit guard had an `and hw_accel == CPU` condition,
so VAAPI/NVIDIA/AMD fell through. ffmpeg was called with the same file as both input
and output, which truncates the input to 0 bytes on open. Recording permanently
destroyed.

Fix: drop the hw_accel restriction. Same input/output format is always a no-op
regardless of hw_accel.

Related: test_transcode_ts_hwaccel_noop.py covers the identical bug for TS output
(fixed earlier with a dedicated TS-to-TS guard, now unified into one check).
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


class TranscodeMp4MkvHwAccelNoopTests(unittest.IsolatedAsyncioTestCase):
    """transcode() must return the input path unchanged for same-format output, regardless of hw_accel."""

    async def _assert_same_format_skips_ffmpeg(
        self, suffix: str, output_format: "OutputFormat", hw_accel: "HardwareAccel"
    ):
        processor = PostProcessor()
        processor._ffmpeg_path = "ffmpeg"
        file_content = b"\x00" * 4096

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(file_content)
            tmp_path = f.name

        try:
            with patch.object(
                processor, "_run_ffmpeg_with_progress", new_callable=AsyncMock
            ) as mock_ffmpeg:
                result = await processor.transcode(
                    tmp_path, output_format, hw_accel=hw_accel
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

    # MP4 cases

    async def test_mp4_to_mp4_cpu_skips_ffmpeg(self):
        """Baseline: CPU already worked before the fix."""
        await self._assert_same_format_skips_ffmpeg(".mp4", OutputFormat.MP4, HardwareAccel.CPU)

    async def test_mp4_to_mp4_vaapi_skips_ffmpeg(self):
        """VAAPI must not call ffmpeg for MP4-to-MP4 (was data-loss bug)."""
        await self._assert_same_format_skips_ffmpeg(".mp4", OutputFormat.MP4, HardwareAccel.VAAPI)

    async def test_mp4_to_mp4_nvidia_skips_ffmpeg(self):
        """NVIDIA must not call ffmpeg for MP4-to-MP4."""
        await self._assert_same_format_skips_ffmpeg(".mp4", OutputFormat.MP4, HardwareAccel.NVIDIA)

    async def test_mp4_to_mp4_amd_skips_ffmpeg(self):
        """AMD must not call ffmpeg for MP4-to-MP4."""
        await self._assert_same_format_skips_ffmpeg(".mp4", OutputFormat.MP4, HardwareAccel.AMD)

    # MKV cases

    async def test_mkv_to_mkv_cpu_skips_ffmpeg(self):
        """Baseline: CPU already worked before the fix."""
        await self._assert_same_format_skips_ffmpeg(".mkv", OutputFormat.MKV, HardwareAccel.CPU)

    async def test_mkv_to_mkv_vaapi_skips_ffmpeg(self):
        """VAAPI must not call ffmpeg for MKV-to-MKV (was data-loss bug)."""
        await self._assert_same_format_skips_ffmpeg(".mkv", OutputFormat.MKV, HardwareAccel.VAAPI)

    async def test_mkv_to_mkv_nvidia_skips_ffmpeg(self):
        """NVIDIA must not call ffmpeg for MKV-to-MKV."""
        await self._assert_same_format_skips_ffmpeg(".mkv", OutputFormat.MKV, HardwareAccel.NVIDIA)

    async def test_mkv_to_mkv_amd_skips_ffmpeg(self):
        """AMD must not call ffmpeg for MKV-to-MKV."""
        await self._assert_same_format_skips_ffmpeg(".mkv", OutputFormat.MKV, HardwareAccel.AMD)


if __name__ == "__main__":
    unittest.main()
