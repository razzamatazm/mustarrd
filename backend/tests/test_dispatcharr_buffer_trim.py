"""Tail trimming never installs an unverified candidate or removes the beginning."""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.post_processor import PostProcessor
from services.process_runner import CommandResult, ProcessRunner


class BufferTrimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "Recording.ts"
        self.source.write_bytes(b"original recording")
        self.source.chmod(0o640)
        self.unrelated = Path(self.folder.name) / "another-file.ts"
        self.unrelated.write_bytes(b"not owned by this trim")
        self.runner = ProcessRunner()
        self.processor = PostProcessor(self.runner)
        self.ffmpeg_result = CommandResult(0)
        self.probe_result = CommandResult(0, json.dumps({
            "format": {"duration": "3600.04"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }).encode())
        self.candidate_bytes = b"validated trimmed recording"

        async def run(cmd, **kwargs):
            if cmd[0] == "/ffmpeg":
                Path(cmd[-1]).write_bytes(self.candidate_bytes)
                if isinstance(self.ffmpeg_result, BaseException):
                    raise self.ffmpeg_result
                return self.ffmpeg_result
            if isinstance(self.probe_result, BaseException):
                raise self.probe_result
            return self.probe_result

        self.run = AsyncMock(side_effect=run)
        self.addCleanup(patch.stopall)
        patch.object(self.runner, "run_capture", self.run).start()
        self.duration = patch.object(self.processor, "_get_duration", AsyncMock(return_value=3900.0)).start()
        patch.object(self.processor, "get_ffmpeg_path", return_value="/ffmpeg").start()
        patch.object(self.processor, "_resolve_ffprobe_path", return_value="/ffprobe").start()

    def assert_preserved(self):
        self.assertEqual(self.source.read_bytes(), b"original recording")
        self.assertEqual(self.unrelated.read_bytes(), b"not owned by this trim")
        self.assertEqual(list(Path(self.folder.name).glob(".Recording.buffer-trim-*")), [])

    async def test_copy_all_streams_and_replace_only_valid_candidate(self):
        self.assertTrue(await self.processor.trim_buffer_tail(str(self.source), 3600))
        cmd = self.run.call_args_list[0].args[0]
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.source))
        self.assertEqual(cmd[cmd.index("-map") + 1], "0")
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertIn("-copyinkf", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "3600.000000")
        self.assertNotIn("-ss", cmd)
        self.assertEqual(Path(cmd[-1]).parent, self.source.parent)
        self.assertEqual(Path(cmd[-1]).suffix, ".ts")
        self.assertNotEqual(cmd[-1], str(self.source))
        self.assertEqual(self.source.read_bytes(), self.candidate_bytes)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o640)
        self.assertFalse(Path(cmd[-1]).exists())

    async def test_recovery_after_successful_trim_is_a_noop(self):
        self.duration.return_value = 3600.04
        self.assertFalse(await self.processor.trim_buffer_tail(str(self.source), 3600))
        self.run.assert_not_awaited()
        self.assert_preserved()

    async def test_current_short_unknown_or_unreliable_measurement_never_trims(self):
        for current in (3300, 0, float("nan"), 93600):
            with self.subTest(current=current):
                self.duration.return_value = current
                with self.assertRaisesRegex(RuntimeError, "no reliably measured excess"):
                    await self.processor.trim_buffer_tail(str(self.source), 3600)
                self.run.assert_not_awaited()
                self.assert_preserved()

    async def test_failed_ffmpeg_keeps_original_and_cleans_only_its_temp(self):
        self.ffmpeg_result = CommandResult(1, stderr=b"write failed")
        with self.assertRaisesRegex(RuntimeError, "FFmpeg could not trim"):
            await self.processor.trim_buffer_tail(str(self.source), 3600)
        self.assertEqual(self.run.await_count, 1)
        self.assert_preserved()

    async def test_empty_candidate_is_rejected(self):
        self.candidate_bytes = b""
        with self.assertRaisesRegex(RuntimeError, "no usable file"):
            await self.processor.trim_buffer_tail(str(self.source), 3600)
        self.assert_preserved()

    async def test_invalid_duration_or_missing_av_streams_is_rejected(self):
        candidates = [
            {"format": {"duration": value}, "streams": [{"codec_type": "video"}]}
            for value in ("0", "nan", "inf", "3300", "3900")
        ] + [
            {"format": {"duration": "3600"}, "streams": streams}
            for streams in ([], [{"codec_type": "subtitle"}])
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.probe_result = CommandResult(0, json.dumps(candidate).encode())
                with self.assertRaisesRegex(RuntimeError, "stream and duration checks"):
                    await self.processor.trim_buffer_tail(str(self.source), 3600)
                self.assert_preserved()

    async def test_failed_unreadable_or_timed_out_probe_is_rejected(self):
        for result in (CommandResult(1), CommandResult(None, timed_out=True), CommandResult(0, b"invalid")):
            with self.subTest(result=result):
                self.probe_result = result
                with self.assertRaises(RuntimeError):
                    await self.processor.trim_buffer_tail(str(self.source), 3600)
                self.assert_preserved()

    async def test_cancellation_during_ffmpeg_or_validation_cleans_owned_temp(self):
        for stage in ("ffmpeg", "probe"):
            with self.subTest(stage=stage):
                if stage == "ffmpeg":
                    self.ffmpeg_result = asyncio.CancelledError()
                else:
                    self.ffmpeg_result = CommandResult(0)
                    self.probe_result = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.processor.trim_buffer_tail(str(self.source), 3600)
                self.assert_preserved()

    async def test_replace_failure_keeps_original(self):
        with patch("services.post_processor.os.replace", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                await self.processor.trim_buffer_tail(str(self.source), 3600)
        self.assert_preserved()

    async def test_missing_ffmpeg_never_creates_candidate(self):
        with patch.object(self.processor, "get_ffmpeg_path", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requires FFmpeg and ffprobe"):
                await self.processor.trim_buffer_tail(str(self.source), 3600)
        self.run.assert_not_awaited()
        self.assert_preserved()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe are required")
class RealBufferTrimTests(unittest.IsolatedAsyncioTestCase):
    async def _first_packet(self, runner, source):
        probe = await runner.run_capture([
            shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "%+#1", "-show_packets", "-show_data_hash", "sha256",
            "-of", "json", str(source),
        ], timeout=10)
        self.assertEqual(probe.returncode, 0, probe.stderr_text)
        return json.loads(probe.stdout_text)["packets"][0]

    async def test_real_stream_copy_retains_first_packet_and_trims_tail(self):
        runner = ProcessRunner()
        processor = PostProcessor(runner)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "nonuniform.ts"
            generated = await runner.run_capture([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=40",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000:duration=40",
                "-c:v", "mpeg2video", "-g", "10", "-c:a", "mp2", "-b:a", "64k",
                "-f", "mpegts", "-y", str(source),
            ], timeout=30)
            self.assertEqual(generated.returncode, 0, generated.stderr_text)

            before_hash = (await self._first_packet(runner, source))["data_hash"]
            before = await processor._get_duration(str(source))
            self.assertGreater(before, 39)
            self.assertTrue(await processor.trim_buffer_tail(str(source), 30))
            self.assertEqual((await self._first_packet(runner, source))["data_hash"], before_hash)
            after = await processor._get_duration(str(source))
            self.assertAlmostEqual(after, 30, delta=1)
            self.assertLess(after, before - 8)
            media = await runner.run_capture([
                shutil.which("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "json", str(source),
            ], timeout=10)
            self.assertEqual(
                {stream["codec_type"] for stream in json.loads(media.stdout_text)["streams"]},
                {"audio", "video"},
            )
            self.assertFalse(await processor.trim_buffer_tail(str(source), 30))

    async def _video_packets(self, runner, source):
        probe = await runner.run_capture([
            shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "json", str(source),
        ], timeout=20)
        self.assertEqual(probe.returncode, 0, probe.stderr_text)
        return int(json.loads(probe.stdout_text)["streams"][0]["nb_read_packets"])

    async def test_a_timestamp_jump_is_not_mistaken_for_extra_footage(self):
        """A provider file whose clock skips ahead measures long without being long.

        Thirty seconds of footage with an 8 second skip in its timestamps
        probes as 38 seconds. Cutting that "back" to 30 by the clock would
        throw away 8 seconds of the show and still pass the length check.
        """
        runner = ProcessRunner()
        processor = PostProcessor(runner)
        with tempfile.TemporaryDirectory() as folder:
            parts = []
            for name, seconds, offset in (("before", 5, 0), ("after", 25, 13)):
                part = Path(folder) / f"{name}.ts"
                generated = await runner.run_capture([
                    shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=96x64:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=16000:duration={seconds}",
                    "-c:v", "mpeg2video", "-g", "10", "-c:a", "mp2", "-b:a", "64k",
                    "-output_ts_offset", str(offset), "-f", "mpegts", "-y", str(part),
                ], timeout=30)
                self.assertEqual(generated.returncode, 0, generated.stderr_text)
                parts.append(part)
            source = Path(folder) / "skipping-clock.ts"
            source.write_bytes(b"".join(part.read_bytes() for part in parts))

            footage = await self._video_packets(runner, source)
            self.assertGreater(await processor._get_duration(str(source)), 36)

            try:
                await processor.trim_buffer_tail(str(source), 30)
            except RuntimeError:
                pass  # Refusing to touch the file is also a safe answer.

            self.assertGreaterEqual(
                await self._video_packets(runner, source), footage - 5,
                "The trim removed footage that was never extra.",
            )

    async def test_mid_gop_recording_retains_initial_non_key_packets(self):
        runner = ProcessRunner()
        processor = PostProcessor(runner)
        with tempfile.TemporaryDirectory() as folder:
            full = Path(folder) / "full.ts"
            source = Path(folder) / "mid-gop.ts"
            generated = await runner.run_capture([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=40",
                "-c:v", "mpeg2video", "-g", "40", "-bf", "0", "-an",
                "-f", "mpegts", "-y", str(full),
            ], timeout=30)
            self.assertEqual(generated.returncode, 0, generated.stderr_text)
            probe = await runner.run_capture([
                shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_packets", "-of", "json", str(full),
            ], timeout=10)
            packets = json.loads(probe.stdout_text)["packets"]
            # Catchup streams can begin between keyframes. Cutting at a TS
            # packet boundary recreates that input without asking FFmpeg to
            # select a later keyframe while preparing the test itself.
            source.write_bytes(full.read_bytes()[int(packets[5]["pos"]):])
            first = await self._first_packet(runner, source)
            self.assertNotIn("K", first["flags"])
            self.assertTrue(await processor.trim_buffer_tail(str(source), 30))
            self.assertEqual((await self._first_packet(runner, source))["data_hash"], first["data_hash"])
            self.assertAlmostEqual(await processor._get_duration(str(source)), 30, delta=1)


if __name__ == "__main__":
    unittest.main()
