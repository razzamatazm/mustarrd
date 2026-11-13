"""
Regression test: remove_commercials produces incorrect output when ffprobe is
unavailable and duration=0.

Root cause: when _get_duration returns 0 (ffprobe not found or failed), the
omit_to flag in the segment extraction loop fires for every segment, causing
ffmpeg to copy from the segment start to end-of-file instead of to the
commercial boundary. The commercial is never cut and the output contains the
same content as the input. Status is marked COMPLETED with no error.

First fix: remove_commercials raised when duration <= 0, failing the whole
download.

Current behavior: remove_commercials raises CommercialRemovalSkipped instead.
The download manager keeps the recording uncut, records a warning naming
ffprobe, and finishes the rest of post-processing. A scheduled recording
length is NOT used as a stand-in here: it is a guess about the file, and
cutting against a guess risks removing the wrong parts of the show.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("pp_ffprobe_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

HardwareAccel = POST_PROCESSOR_MODULE.HardwareAccel
OutputFormat = POST_PROCESSOR_MODULE.OutputFormat
PostProcessor = POST_PROCESSOR_MODULE.PostProcessor
CommercialRemovalSkipped = POST_PROCESSOR_MODULE.CommercialRemovalSkipped


class CommercialRemovalFfprobeRequiredTests(unittest.IsolatedAsyncioTestCase):
    """remove_commercials must skip the cut when ffprobe duration is unavailable."""

    def setUp(self):
        self.processor = PostProcessor()
        self.processor._ffmpeg_path = "/usr/bin/ffmpeg"
        self._ffmpeg_patcher = patch.object(
            type(self.processor), "ffmpeg_available", new_callable=PropertyMock, return_value=True
        )
        self._ffmpeg_patcher.start()
        self.addCleanup(self._ffmpeg_patcher.stop)

    def _write_edl(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".edl", delete=False)
        f.write(content)
        f.close()
        # A skipped cut cleans up Comskip's outputs, the EDL included.
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    async def test_skips_when_duration_zero_ffprobe_missing(self):
        """When _get_duration returns 0 (ffprobe absent), the cut is skipped.

        duration=0 used to make every ffmpeg segment copy from its start to
        end-of-file, so nothing was cut. The skip signal must name ffprobe so
        the operator knows what to install.
        """
        edl = self._write_edl("1200.000000 1800.000000 0\n")  # commercial at 20-30min

        with patch.object(self.processor, "_get_duration", new=AsyncMock(return_value=0.0)):
            with self.assertRaises(CommercialRemovalSkipped) as ctx:
                await self.processor.remove_commercials(
                    "/tmp/Show.ts",
                    edl,
                    output_format=OutputFormat.TS,
                )

        self.assertIn(
            "ffprobe",
            str(ctx.exception).lower(),
            "Error message must mention ffprobe so the operator knows what to install.",
        )

    async def test_skips_when_duration_negative(self):
        """Negative duration (edge case from ffprobe parse failure) must also skip."""
        edl = self._write_edl("600.000000 1200.000000 0\n")

        with patch.object(self.processor, "_get_duration", new=AsyncMock(return_value=-1.0)):
            with self.assertRaises(CommercialRemovalSkipped):
                await self.processor.remove_commercials(
                    "/tmp/Show.ts",
                    edl,
                    output_format=OutputFormat.TS,
                )

    async def test_scheduled_length_is_not_used_to_cut_when_ffprobe_missing(self):
        """A scheduled length must not stand in for a missing ffprobe duration.

        The schedule says how long the provider was asked for, not what is in
        the file. Cutting against it could remove the wrong parts, so the cut
        is skipped before any segment is extracted.
        """
        edl = self._write_edl("1200.000000 1800.000000 0\n")
        select_maps = AsyncMock(return_value=["-map", "0:v:0"])

        with (
            patch.object(self.processor, "_get_duration", new=AsyncMock(return_value=0.0)),
            patch.object(self.processor, "_select_best_av_map_args", select_maps),
        ):
            with self.assertRaises(CommercialRemovalSkipped):
                await self.processor.remove_commercials(
                    "/tmp/Show.ts",
                    edl,
                    output_format=OutputFormat.MKV,
                    expected_duration_seconds=3600,
                )

        select_maps.assert_not_called()

    async def test_no_segments_skips_duration_check(self):
        """When EDL has no commercials, the duration check is never reached (transcode path).

        This ensures the fix does not break the no-commercial fast path, which
        delegates to transcode() and does not need duration for commercial removal.
        """
        edl = self._write_edl("")  # no commercials

        mock_transcode = AsyncMock(return_value="/tmp/Show.ts")
        with (
            patch.object(self.processor, "transcode", mock_transcode),
            patch.object(self.processor, "_cleanup_comskip_outputs"),
        ):
            result = await self.processor.remove_commercials(
                "/tmp/Show.ts",
                edl,
                output_format=OutputFormat.TS,
            )

        mock_transcode.assert_called_once()
        self.assertEqual(result, "/tmp/Show.ts")

    async def test_valid_duration_does_not_raise_at_duration_check(self):
        """When ffprobe returns a valid duration, remove_commercials passes the guard.

        Regression guard: the fix must not add a new raise for valid durations.
        We stub _select_best_av_map_args to abort early so the test does not
        need a real ffmpeg binary.
        """
        edl = self._write_edl("1200.000000 1800.000000 0\n")
        sentinel = RuntimeError("abort-after-duration-check")

        with (
            patch.object(self.processor, "_get_duration", new=AsyncMock(return_value=3600.0)),
            patch.object(
                self.processor,
                "_select_best_av_map_args",
                new=AsyncMock(side_effect=sentinel),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await self.processor.remove_commercials(
                    "/tmp/Show.ts",
                    edl,
                    output_format=OutputFormat.TS,
                )

        self.assertIs(
            ctx.exception,
            sentinel,
            "Expected the sentinel abort, not an early raise from the duration guard.",
        )


if __name__ == "__main__":
    unittest.main()
