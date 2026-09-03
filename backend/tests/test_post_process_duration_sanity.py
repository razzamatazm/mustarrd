"""Post-processing duration sanity checks for broken IPTV timestamps."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MODULE_PATH = BACKEND_ROOT / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_duration_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

PostProcessor = POST_PROCESSOR_MODULE.PostProcessor
OutputFormat = POST_PROCESSOR_MODULE.OutputFormat


class PostProcessDurationSanityTests(unittest.IsolatedAsyncioTestCase):
    async def test_implausible_probe_uses_scheduled_duration(self):
        processor = PostProcessor()
        messages = []
        with patch.object(
            processor,
            "_get_duration",
            new=AsyncMock(return_value=93897.491022),
        ):
            duration = await processor._resolve_duration(
                "/recordings/news.ts",
                expected_duration_seconds=65 * 60,
                log_callback=messages.append,
            )

        self.assertEqual(duration, 65 * 60)
        self.assertTrue(any("implausible duration" in message for message in messages))
        self.assertTrue(any("scheduled length" in message for message in messages))

    async def test_reasonable_probe_remains_authoritative(self):
        processor = PostProcessor()
        with patch.object(
            processor,
            "_get_duration",
            new=AsyncMock(return_value=67 * 60),
        ):
            duration = await processor._resolve_duration(
                "/recordings/news.ts",
                expected_duration_seconds=65 * 60,
            )

        self.assertEqual(duration, 67 * 60)

    async def test_unavailable_probe_uses_scheduled_duration(self):
        processor = PostProcessor()
        messages = []
        with patch.object(
            processor,
            "_get_duration",
            new=AsyncMock(return_value=0),
        ):
            duration = await processor._resolve_duration(
                "/recordings/news.ts",
                expected_duration_seconds=30 * 60,
                log_callback=messages.append,
            )

        self.assertEqual(duration, 30 * 60)
        self.assertTrue(any("duration is unavailable" in message for message in messages))

    async def test_without_schedule_probe_result_is_unchanged(self):
        processor = PostProcessor()
        with patch.object(
            processor,
            "_get_duration",
            new=AsyncMock(return_value=93897.491022),
        ):
            duration = await processor._resolve_duration("/recordings/news.ts")

        self.assertEqual(duration, 93897.491022)

    async def test_transcode_progress_receives_scheduled_duration_for_broken_probe(self):
        processor = PostProcessor()
        processor._ffmpeg_path = "/usr/bin/ffmpeg"
        runner_durations = []

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "News.ts"
            input_path.write_bytes(b"fake ts")
            output_path = Path(tmpdir) / "News.mkv"

            async def fake_run(cmd, duration, callback=None, env=None):
                runner_durations.append(duration)
                output_path.write_bytes(b"fake mkv")
                return 0, b""

            with (
                patch.object(
                    processor,
                    "_get_duration",
                    new=AsyncMock(return_value=93897.491022),
                ),
                patch.object(
                    processor,
                    "_select_best_av_map_args",
                    new=AsyncMock(return_value=["-map", "0:v:0", "-map", "0:a:0?"]),
                ),
                patch.object(
                    processor,
                    "_resolve_ffmpeg_path",
                    return_value="/usr/bin/ffmpeg",
                ),
                patch.object(processor, "_run_ffmpeg_with_progress", side_effect=fake_run),
            ):
                await processor.transcode(
                    str(input_path),
                    OutputFormat.MKV,
                    expected_duration_seconds=65 * 60,
                )

        self.assertEqual(runner_durations, [65 * 60])


if __name__ == "__main__":
    unittest.main()
