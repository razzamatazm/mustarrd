"""
Regression test: {stem}_postproc_tmp.ts orphaned on concat failure when
commercial removal uses TS output format (same_path_output=True).

Bug: remove_commercials creates {stem}_postproc_tmp.{ext} when input and output
share the same extension (e.g. transcode_format=ts).  If FFmpeg concat fails,
the finally block cleans _seg*.ts and .concat.txt but NOT work_output_path.
_cleanup_working_files in download_manager also has no *_postproc_tmp.* pattern.
Result: partial temp file accumulates in the working directory indefinitely.

Distinct from the two existing temp-file orphan bugs:
- "Comskip and FFmpeg temp files orphaned on failure/cancel": covers the path
  where _cleanup_working_files is never called at all.
- "Partial transcode output orphaned on warning path": covers {stem}.mkv/.mp4
  missed by cleanup patterns.
Here the finally block DOES run and DOES clean segments, but misses
work_output_path.

This test FAILS on the current codebase.
"""
import asyncio
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_orphan_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor
OutputFormat = PP_MODULE.OutputFormat
HardwareAccel = PP_MODULE.HardwareAccel


class PostprocTmpOrphanTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_postproc_tmp_cleaned_on_concat_failure_ts_output(self):
        """
        {stem}_postproc_tmp.ts must be deleted when FFmpeg concat fails during
        commercial removal with TS output format (same_path_output=True).

        This test FAILS on the current codebase: the finally block in
        remove_commercials omits work_output_path, leaving the partial file on disk.
        """
        ts_path = Path(self.tmp) / "Show.ts"
        ts_path.write_bytes(b"\x47" * 188)

        edl_path = Path(self.tmp) / "Show.edl"
        edl_path.write_text("60.0 120.0 0\n")

        work_output_path = Path(self.tmp) / "Show_postproc_tmp.ts"

        processor = PostProcessor()
        processor._ffmpeg_path = "/usr/bin/ffmpeg"

        async def fake_run_ffmpeg_with_progress(cmd, *args, **kwargs):
            work_output_path.write_bytes(b"\x00" * 512)
            return (1, b"fake concat fail")

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch.object(
            type(processor), "ffmpeg_available",
            new_callable=PropertyMock, return_value=True,
        ), patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as mock_exec, patch.object(
            processor, "_get_duration",
            new_callable=AsyncMock, return_value=3600.0,
        ), patch.object(
            processor, "_select_best_av_map_args",
            new_callable=AsyncMock, return_value=[],
        ), patch.object(
            processor, "_run_ffmpeg_with_progress",
            side_effect=fake_run_ffmpeg_with_progress,
        ):
            mock_exec.return_value = fake_proc

            with self.assertRaises(Exception, msg="remove_commercials must raise on concat failure"):
                await processor.remove_commercials(
                    str(ts_path),
                    str(edl_path),
                    output_format=OutputFormat.TS,
                    hw_accel=HardwareAccel.CPU,
                )

        self.assertFalse(
            work_output_path.exists(),
            "Show_postproc_tmp.ts was not cleaned up after concat failure. "
            "The finally block in remove_commercials omits work_output_path, "
            "leaving the partial temp file on disk indefinitely. "
            "Fix: unlink work_output_path in the finally block when same_path_output=True.",
        )


if __name__ == "__main__":
    unittest.main()
