import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.hls_streamer as hls_module
from services.hls_streamer import (
    HLS_ASSET_PATTERN,
    HLSLimitError,
    HLSSession,
    HLSStartError,
    HLSStreamer,
)


class AssetPatternTests(unittest.TestCase):
    def test_valid_assets(self):
        for name in ["playlist.m3u8", "init.mp4", "seg00000.m4s", "seg99999.m4s"]:
            self.assertIsNotNone(HLS_ASSET_PATTERN.match(name), name)

    def test_invalid_assets(self):
        for name in [
            "../playlist.m3u8",
            "playlist.m3u8/..",
            "ffmpeg.log",
            "seg1.m4s",
            "seg000000.m4s",
            "seg00000.ts",
            "init.mp4.bak",
            "",
            "playlist.m3u8\n",
        ]:
            self.assertIsNone(HLS_ASSET_PATTERN.match(name), name)


class FfmpegCommandTests(unittest.TestCase):
    def _cmd(self, video, audio):
        return HLSStreamer.build_ffmpeg_command("ffmpeg", Path("/x/in.ts"), video, audio)

    def test_h264_aac_copies_both(self):
        cmd = self._cmd("h264", "aac")
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertNotIn("-tag:v", cmd)

    def test_hevc_copy_gets_hvc1_tag(self):
        cmd = self._cmd("hevc", "aac")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")

    def test_mpeg2_video_transcodes_to_h264(self):
        cmd = self._cmd("mpeg2video", "aac")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)

    def test_aac_copy_applies_adts_filter(self):
        """ADTS AAC from MPEG-TS breaks fMP4 muxing without aac_adtstoasc."""
        cmd = self._cmd("h264", "aac")
        self.assertEqual(cmd[cmd.index("-bsf:a") + 1], "aac_adtstoasc")

    def test_ac3_audio_transcodes_to_aac(self):
        cmd = self._cmd("h264", "ac3")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertNotIn("-bsf:a", cmd)

    def test_mp2_audio_transcodes_to_aac(self):
        cmd = self._cmd("h264", "mp2")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_no_audio_stream_omits_audio_codec(self):
        cmd = self._cmd("h264", None)
        self.assertNotIn("-c:a", cmd)

    def test_fmp4_event_playlist(self):
        cmd = self._cmd("h264", "aac")
        self.assertEqual(cmd[cmd.index("-hls_segment_type") + 1], "fmp4")
        self.assertEqual(cmd[cmd.index("-hls_playlist_type") + 1], "event")
        self.assertEqual(cmd[-1], "playlist.m3u8")


class ProbeParseTests(unittest.TestCase):
    def test_picks_first_video_and_audio(self):
        raw = (
            '{"streams": ['
            '{"codec_type": "video", "codec_name": "h264"},'
            '{"codec_type": "audio", "codec_name": "ac3"},'
            '{"codec_type": "audio", "codec_name": "aac"},'
            '{"codec_type": "subtitle", "codec_name": "dvb_subtitle"}'
            "]}"
        )
        self.assertEqual(HLSStreamer._parse_probe_output(raw), ("h264", "ac3"))

    def test_missing_streams_returns_nones(self):
        self.assertEqual(HLSStreamer._parse_probe_output('{"streams": []}'), (None, None))

    def test_garbage_raises(self):
        with self.assertRaises(HLSStartError):
            HLSStreamer._parse_probe_output("not json")


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, streamer, download_id, source, age_seconds=0.0):
        directory = Path(tempfile.mkdtemp(prefix="hls-test-"))
        session = HLSSession(
            download_id=download_id,
            source_path=Path(source),
            directory=directory,
        )
        session.last_access = time.monotonic() - age_seconds
        streamer._sessions[download_id] = session
        return session

    async def test_get_or_create_reuses_matching_session(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        result = await streamer.get_or_create(1, Path("/media/a.ts"))
        self.assertIs(result, session)
        self.assertTrue(session.directory.exists())
        await streamer.shutdown()

    async def test_failed_session_is_not_reused(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        session.failed_reason = "boom"
        with patch("services.hls_streamer.post_processor") as mock_pp:
            mock_pp.get_ffmpeg_path.return_value = None
            mock_pp.get_ffprobe_path.return_value = None
            with self.assertRaises(hls_module.HLSUnavailableError):
                await streamer.get_or_create(1, Path("/media/a.ts"))
        # Stale failed session was torn down, including its directory.
        self.assertNotIn(1, streamer._sessions)
        self.assertFalse(session.directory.exists())
        await streamer.shutdown()

    async def test_crashed_process_session_is_not_reused(self):
        """A session whose FFmpeg exited nonzero must be rebuilt, not served."""
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")

        class DeadProcess:
            returncode = 1

        session.process = DeadProcess()
        with patch("services.hls_streamer.post_processor") as mock_pp:
            mock_pp.get_ffmpeg_path.return_value = None
            mock_pp.get_ffprobe_path.return_value = None
            with self.assertRaises(hls_module.HLSUnavailableError):
                await streamer.get_or_create(1, Path("/media/a.ts"))
        self.assertNotIn(1, streamer._sessions)
        self.assertFalse(session.directory.exists())
        await streamer.shutdown()

    async def test_session_limit(self):
        streamer = HLSStreamer()
        for i in range(hls_module.MAX_SESSIONS):
            self._make_session(streamer, i + 1, f"/media/{i}.ts")
        with self.assertRaises(HLSLimitError):
            await streamer.get_or_create(99, Path("/media/new.ts"))
        await streamer.shutdown()

    async def test_idle_sessions_are_reaped(self):
        streamer = HLSStreamer()
        idle = self._make_session(
            streamer, 1, "/media/a.ts", age_seconds=hls_module.IDLE_TTL_SECONDS + 5
        )
        fresh = self._make_session(streamer, 2, "/media/b.ts")
        async with streamer._lock:
            await streamer._reap_idle_locked()
        self.assertNotIn(1, streamer._sessions)
        self.assertFalse(idle.directory.exists())
        self.assertIn(2, streamer._sessions)
        self.assertTrue(fresh.directory.exists())
        await streamer.shutdown()

    async def test_get_active_skips_failed(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        self.assertIs(streamer.get_active(1), session)
        session.failed_reason = "boom"
        self.assertIsNone(streamer.get_active(1))
        self.assertIsNone(streamer.get_active(42))
        await streamer.shutdown()

    async def test_shutdown_removes_everything(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        await streamer.shutdown()
        self.assertEqual(streamer._sessions, {})
        self.assertFalse(session.directory.exists())


class WaitForPlaylistTests(unittest.IsolatedAsyncioTestCase):
    def _session_with_dir(self):
        directory = Path(tempfile.mkdtemp(prefix="hls-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return HLSSession(download_id=1, source_path=Path("/media/a.ts"), directory=directory)

    async def test_returns_once_playlist_has_segment(self):
        session = self._session_with_dir()
        session.playlist_path.write_text(
            "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.0,\nseg00000.m4s\n"
        )
        streamer = HLSStreamer()
        await streamer.wait_for_playlist(session)  # must not raise

    async def test_failed_process_raises_with_log_tail(self):
        session = self._session_with_dir()

        class DeadProcess:
            returncode = 1

        session.process = DeadProcess()
        (session.directory / "ffmpeg.log").write_text("something\nInvalid data found\n")
        streamer = HLSStreamer()
        with self.assertRaises(HLSStartError) as ctx:
            await streamer.wait_for_playlist(session)
        self.assertIn("Invalid data found", str(ctx.exception))
        self.assertIsNotNone(session.failed_reason)

    async def test_crashed_process_with_truncated_playlist_still_fails(self):
        """FFmpeg can die after writing a segment reference; that playlist
        must not be reported as ready."""
        session = self._session_with_dir()

        class DeadProcess:
            returncode = 234

        session.process = DeadProcess()
        session.playlist_path.write_text(
            "#EXTM3U\n#EXTINF:0.28,\nseg00000.m4s\n#EXT-X-ENDLIST\n"
        )
        (session.directory / "ffmpeg.log").write_text("Error muxing a packet\n")
        streamer = HLSStreamer()
        with self.assertRaises(HLSStartError) as ctx:
            await streamer.wait_for_playlist(session)
        self.assertIn("Error muxing a packet", str(ctx.exception))

    async def test_timeout_marks_session_failed(self):
        session = self._session_with_dir()
        streamer = HLSStreamer()
        with patch.object(hls_module, "PLAYLIST_WAIT_SECONDS", 0.3):
            with self.assertRaises(HLSStartError):
                await streamer.wait_for_playlist(session)
        self.assertIsNotNone(session.failed_reason)


if __name__ == "__main__":
    unittest.main()
