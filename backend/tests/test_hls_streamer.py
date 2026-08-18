import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.hls_streamer as hls_module
from services.hls_streamer import (
    HLS_ASSET_PATTERN,
    download_session_key,
    HLSLimitError,
    HLSSession,
    HLSStartError,
    HLSStreamer,
    PIPE_INPUT,
    assert_loopback_url,
    preview_session_key,
    vod_preview_session_key,
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
    def _cmd(self, video, audio, profile="LC", start=0.0):
        return HLSStreamer.build_ffmpeg_command(
            "ffmpeg", Path("/x/in.ts"), video, audio, profile, start
        )

    def test_h264_aac_lc_copies_both(self):
        cmd = self._cmd("h264", "aac", "LC")
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

    def test_he_aac_transcodes_to_lc(self):
        """Firefox MSE rejects non-LC AAC codec strings (bufferAddCodecError)."""
        cmd = self._cmd("h264", "aac", "HE-AAC")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertNotIn("-bsf:a", cmd)

    def test_main_profile_aac_transcodes_to_lc(self):
        """Provider ADTS headers mislabeled as AAC Main become mp4a.40.1
        under -c:a copy, which Firefox refuses to buffer."""
        cmd = self._cmd("h264", "aac", "Main")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_unknown_aac_profile_transcodes_to_lc(self):
        cmd = self._cmd("h264", "aac", None)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_start_offset_seeks_before_input(self):
        cmd = self._cmd("h264", "aac", "LC", start=93.5)
        ss = cmd.index("-ss")
        self.assertEqual(cmd[ss + 1], "93.500")
        self.assertLess(ss, cmd.index("-i"))

    def test_zero_start_offset_omits_seek(self):
        cmd = self._cmd("h264", "aac", "LC", start=0.0)
        self.assertNotIn("-ss", cmd)

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

    def test_file_source_uses_six_second_segments(self):
        cmd = self._cmd("h264", "aac")
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "6")


class LiveFfmpegCommandTests(unittest.TestCase):
    """Converted preview: a provider stream piped in over stdin."""

    def _cmd(self, video, audio, profile=None):
        return HLSStreamer.build_ffmpeg_command(
            "ffmpeg", PIPE_INPUT, video, audio, profile, live=True
        )

    def test_provider_url_never_reaches_argv(self):
        """The provider URL embeds the account username and password; in argv
        it would be readable by every user on the host via `ps`."""
        cmd = self._cmd("h264", "ac3")
        self.assertEqual(cmd[cmd.index("-i") + 1], "pipe:0")
        joined = " ".join(cmd)
        for leak in ("http://", "https://", "username=", "password="):
            self.assertNotIn(leak, joined)

    def test_ac3_source_copies_video_and_reencodes_audio(self):
        """The AC-3 case the fallback exists for: no browser decodes AC-3, but
        the video is H.264 and must not be re-encoded."""
        cmd = self._cmd("h264", "ac3")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_he_aac_source_reencodes_audio(self):
        cmd = self._cmd("h264", "aac", "HE-AAC")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")

    def test_short_segments_for_fast_startup(self):
        cmd = self._cmd("h264", "aac", "LC")
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "2")

    def test_stdin_input_keeps_stdin_open(self):
        """-nostdin would close the pipe FFmpeg is supposed to read from."""
        self.assertNotIn("-nostdin", self._cmd("h264", "aac"))
        self.assertIn("-nostdin", HLSStreamer.build_ffmpeg_command(
            "ffmpeg", Path("/x/in.ts"), "h264", "aac"
        ))


class ProbeParseTests(unittest.TestCase):
    def test_picks_first_video_and_audio(self):
        raw = (
            '{"streams": ['
            '{"codec_type": "video", "codec_name": "h264"},'
            '{"codec_type": "audio", "codec_name": "ac3", "profile": "Dolby Digital"},'
            '{"codec_type": "audio", "codec_name": "aac", "profile": "LC"},'
            '{"codec_type": "subtitle", "codec_name": "dvb_subtitle"}'
            "]}"
        )
        self.assertEqual(
            HLSStreamer._parse_probe_output(raw), ("h264", "ac3", "Dolby Digital", None)
        )

    def test_parses_audio_profile_and_duration(self):
        raw = (
            '{"streams": ['
            '{"codec_type": "video", "codec_name": "h264"},'
            '{"codec_type": "audio", "codec_name": "aac", "profile": "HE-AAC"}'
            '], "format": {"duration": "2641.472000"}}'
        )
        self.assertEqual(
            HLSStreamer._parse_probe_output(raw), ("h264", "aac", "HE-AAC", 2641.472)
        )

    def test_missing_streams_returns_nones(self):
        self.assertEqual(
            HLSStreamer._parse_probe_output('{"streams": []}'), (None, None, None, None)
        )

    def test_garbage_raises(self):
        with self.assertRaises(HLSStartError):
            HLSStreamer._parse_probe_output("not json")


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _make_session(self, streamer, download_id, source, age_seconds=0.0, start_offset=0.0):
        directory = Path(tempfile.mkdtemp(prefix="hls-test-"))
        key = download_session_key(download_id)
        session = HLSSession(
            key=key,
            source_path=Path(source),
            directory=directory,
            start_offset=start_offset,
            fingerprint=f"file:{source}:{start_offset:.3f}",
        )
        session.last_access = time.monotonic() - age_seconds
        streamer._sessions[key] = session
        return session

    async def test_get_or_create_reuses_matching_session(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        result = await streamer.get_or_create_file(download_session_key(1), Path("/media/a.ts"))
        self.assertIs(result, session)
        self.assertTrue(session.directory.exists())
        await streamer.shutdown()

    async def test_different_start_offset_rebuilds_session(self):
        """Seeking outside the produced range requests a new offset; the old
        session must be torn down rather than served from the wrong position."""
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        with patch("services.hls_streamer.post_processor") as mock_pp:
            mock_pp.get_ffmpeg_path.return_value = None
            mock_pp.get_ffprobe_path.return_value = None
            with self.assertRaises(hls_module.HLSUnavailableError):
                await streamer.get_or_create_file(download_session_key(1), Path("/media/a.ts"), start_offset=120.0)
        self.assertNotIn(download_session_key(1), streamer._sessions)
        self.assertFalse(session.directory.exists())
        await streamer.shutdown()

    async def test_failed_session_is_not_reused(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        session.failed_reason = "boom"
        with patch("services.hls_streamer.post_processor") as mock_pp:
            mock_pp.get_ffmpeg_path.return_value = None
            mock_pp.get_ffprobe_path.return_value = None
            with self.assertRaises(hls_module.HLSUnavailableError):
                await streamer.get_or_create_file(download_session_key(1), Path("/media/a.ts"))
        # Stale failed session was torn down, including its directory.
        self.assertNotIn(download_session_key(1), streamer._sessions)
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
                await streamer.get_or_create_file(download_session_key(1), Path("/media/a.ts"))
        self.assertNotIn(download_session_key(1), streamer._sessions)
        self.assertFalse(session.directory.exists())
        await streamer.shutdown()

    async def test_session_limit(self):
        streamer = HLSStreamer()
        for i in range(hls_module.MAX_SESSIONS):
            self._make_session(streamer, i + 1, f"/media/{i}.ts")
        with self.assertRaises(HLSLimitError):
            await streamer.get_or_create_file(download_session_key(99), Path("/media/new.ts"))
        await streamer.shutdown()

    async def test_idle_sessions_are_reaped(self):
        streamer = HLSStreamer()
        idle = self._make_session(
            streamer, 1, "/media/a.ts", age_seconds=hls_module.IDLE_TTL_SECONDS + 5
        )
        fresh = self._make_session(streamer, 2, "/media/b.ts")
        async with streamer._lock:
            await streamer._reap_idle_locked()
        self.assertNotIn(download_session_key(1), streamer._sessions)
        self.assertFalse(idle.directory.exists())
        self.assertIn(download_session_key(2), streamer._sessions)
        self.assertTrue(fresh.directory.exists())
        await streamer.shutdown()

    async def test_get_active_skips_failed(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        self.assertIs(streamer.get_active(download_session_key(1)), session)
        session.failed_reason = "boom"
        self.assertIsNone(streamer.get_active(download_session_key(1)))
        self.assertIsNone(streamer.get_active(download_session_key(42)))
        await streamer.shutdown()

    async def test_shutdown_removes_everything(self):
        streamer = HLSStreamer()
        session = self._make_session(streamer, 1, "/media/a.ts")
        await streamer.shutdown()
        self.assertEqual(streamer._sessions, {})
        self.assertFalse(session.directory.exists())

    def test_root_dir_is_per_process(self):
        """shutdown() rmtree's the whole root; a shared name would let one
        instance wipe another instance's live sessions on the same host."""
        streamer = HLSStreamer()
        self.assertEqual(streamer._root_dir.name, f"mustarrd-hls-{os.getpid()}")

    async def test_cancelled_spawn_cleans_up_dir_and_process(self):
        """Cancellation during the FFmpeg spawn must not leak the session
        temp dir (it was never registered, so nothing else would reap it)."""
        streamer = HLSStreamer()
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_dirs.append(Path(path))
            return path

        fake_process = AsyncMock()
        fake_process.returncode = None
        fake_process.kill = lambda: None

        async def cancelled_spawn(*_args, **_kwargs):
            raise asyncio.CancelledError()

        with (
            patch("services.hls_streamer.tempfile.mkdtemp", tracking_mkdtemp),
            patch("services.hls_streamer.asyncio.create_subprocess_exec", cancelled_spawn),
            patch.object(streamer, "_probe_media", AsyncMock(return_value=("h264", "aac", "LC", None))),
            patch("services.hls_streamer.post_processor") as mock_pp,
        ):
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.get_ffprobe_path.return_value = "/usr/bin/ffprobe"
            with self.assertRaises(asyncio.CancelledError):
                await streamer.get_or_create_file(download_session_key(1), Path("/media/a.ts"))

        self.assertEqual(len(created_dirs), 1)
        self.assertFalse(created_dirs[0].exists(), "session temp dir leaked")
        self.assertNotIn(download_session_key(1), streamer._sessions)
        await streamer.shutdown()


class FakeStreamSource:
    """Stands in for a provider connection: yields TS-ish bytes, records close."""

    def __init__(self, chunks=None, fail=None):
        self._chunks = chunks if chunks is not None else [b"\x47" * 188] * 4
        self._fail = fail
        self.closed = False
        self.opened = False

    async def open(self):
        if self._fail:
            raise self._fail
        self.opened = True

        async def iterator():
            for chunk in self._chunks:
                yield chunk

        return iterator()

    async def close(self):
        self.closed = True


class FakeStdin:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeProcess:
    """Enough of asyncio.subprocess.Process for session lifecycle tests."""

    def __init__(self):
        self.returncode = None
        self.stdin = FakeStdin()
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class StreamSessionTests(unittest.IsolatedAsyncioTestCase):
    """Converted preview sessions: FFmpeg fed from a caller-owned byte stream."""

    async def _start(self, streamer, source, key="preview:1:55", fingerprint="live:None:None:None",
                     on_close=None, probe=("h264", "ac3", None, None), max_seconds=300.0):
        process = FakeProcess()

        async def fake_spawn(session, cmd, stdin_pipe=False):
            self.spawned_cmd = cmd
            self.spawned_stdin_pipe = stdin_pipe
            return process

        with (
            patch.object(streamer, "_spawn_ffmpeg", fake_spawn),
            patch.object(streamer, "_probe_media", AsyncMock(return_value=probe)),
            patch("services.hls_streamer.post_processor") as mock_pp,
        ):
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.get_ffprobe_path.return_value = "/usr/bin/ffprobe"
            session = await streamer.get_or_create_stream(
                key, source, fingerprint, max_seconds, on_close=on_close
            )
        return session, process

    async def test_stream_session_pipes_bytes_into_ffmpeg(self):
        streamer = HLSStreamer()
        source = FakeStreamSource(chunks=[b"\x47" + b"a" * 187, b"\x47" + b"b" * 187])
        session, process = await self._start(streamer, source)

        self.assertTrue(self.spawned_stdin_pipe, "FFmpeg must read the stream from stdin")
        self.assertEqual(self.spawned_cmd[self.spawned_cmd.index("-i") + 1], PIPE_INPUT)
        await asyncio.wait_for(session.feeder, timeout=2)
        # The probe sample is the head of the stream, so it must be replayed
        # into FFmpeg rather than dropped.
        self.assertEqual(bytes(process.stdin.written), b"\x47" + b"a" * 187 + b"\x47" + b"b" * 187)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(source.closed, "provider connection must be released when the feed ends")
        await streamer.shutdown()

    async def test_probe_sample_is_not_left_behind(self):
        streamer = HLSStreamer()
        session, _ = await self._start(streamer, FakeStreamSource())
        self.assertFalse((session.directory / "probe.ts").exists())
        await streamer.shutdown()

    async def test_session_marked_live_and_registered(self):
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        session, _ = await self._start(streamer, FakeStreamSource(), key=key)
        self.assertTrue(session.live)
        self.assertIs(streamer.get_active(key), session)
        self.assertEqual(session.idle_ttl, hls_module.LIVE_IDLE_TTL_SECONDS)
        await streamer.shutdown()

    async def test_on_close_fires_once_when_session_destroyed(self):
        """The preview slot is released through this callback; releasing twice
        would hand out a slot that is still in use."""
        streamer = HLSStreamer()
        calls = []
        key = preview_session_key(1, "55")
        await self._start(streamer, FakeStreamSource(), key=key, on_close=lambda: calls.append(1))

        session = streamer.get_active(key)
        await streamer.close_session(session)
        await streamer.close_session(session)
        await streamer.shutdown()
        self.assertEqual(calls, [1])

    async def test_no_video_track_fails_and_releases_the_source(self):
        streamer = HLSStreamer()
        source = FakeStreamSource()
        calls = []
        with self.assertRaises(HLSStartError):
            await self._start(
                streamer, source, on_close=lambda: calls.append(1), probe=(None, "aac", "LC", None)
            )
        await asyncio.sleep(0)  # the provider close runs as a detached task
        self.assertTrue(source.closed, "provider connection leaked on a failed start")
        self.assertEqual(calls, [1], "reserved preview slot was never released")
        self.assertIsNone(streamer.get_active("preview:1:55"))
        await streamer.shutdown()

    async def test_empty_stream_fails(self):
        streamer = HLSStreamer()
        with self.assertRaises(HLSStartError):
            await self._start(streamer, FakeStreamSource(chunks=[]))
        await streamer.shutdown()

    async def test_reused_session_hands_the_callers_budget_straight_back(self):
        """The caller reserved a preview slot before calling; if an equivalent
        session already existed it never became one, so the streamer returns
        the reservation rather than making callers detect the race."""
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        await self._start(streamer, FakeStreamSource(), key=key)

        calls = []
        second, _ = await self._start(
            streamer, FakeStreamSource(), key=key, on_close=lambda: calls.append(1)
        )

        self.assertEqual(calls, [1], "the losing caller's slot was never released")
        # ...and the winner's own callback is untouched, so tearing the
        # session down later still releases exactly its own slot.
        await streamer.close_session(second)
        self.assertEqual(calls, [1])
        await streamer.shutdown()

    async def test_close_session_ignores_a_replaced_session(self):
        """Closing by key would let a failed request kill the healthy session
        a later request started on the same channel."""
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        stale, _ = await self._start(streamer, FakeStreamSource(), key=key,
                                     fingerprint="catchup:1:2:None")
        replacement, _ = await self._start(streamer, FakeStreamSource(), key=key,
                                           fingerprint="catchup:9:10:None")
        self.assertIsNot(replacement, stale)

        await streamer.close_session(stale)

        self.assertIs(streamer.get_active(key), replacement)
        self.assertTrue(replacement.directory.exists())
        await streamer.shutdown()

    async def test_matching_session_is_reused_without_a_second_ffmpeg(self):
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        first, _ = await self._start(streamer, FakeStreamSource(), key=key)
        second_source = FakeStreamSource()
        second, _ = await self._start(streamer, second_source, key=key)
        self.assertIs(second, first)
        self.assertFalse(second_source.opened, "reused session must not open a second connection")
        await streamer.shutdown()

    async def test_different_program_rebuilds_the_session(self):
        """Same channel, different catchup program: the old session renders
        the wrong window and must not be served."""
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        first, _ = await self._start(streamer, FakeStreamSource(), key=key, fingerprint="catchup:100:200:None")
        second, _ = await self._start(streamer, FakeStreamSource(), key=key, fingerprint="catchup:900:1000:None")
        self.assertIsNot(second, first)
        self.assertFalse(first.directory.exists())
        await streamer.shutdown()

    async def test_finished_live_session_is_not_reused(self):
        """A live session that hit its time cap has an ENDLIST playlist; it
        would replay a stale window instead of showing live TV."""
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        first, process = await self._start(streamer, FakeStreamSource(), key=key)
        process.returncode = 0
        second, _ = await self._start(streamer, FakeStreamSource(), key=key)
        self.assertIsNot(second, first)
        await streamer.shutdown()

    async def test_silent_provider_still_hits_the_time_cap(self):
        """The cap is a deadline on the whole feed: a provider that goes quiet
        must not be able to hold a preview slot open past it by sending
        nothing at all."""
        streamer = HLSStreamer()

        async def silent():
            # Enough to satisfy the probe, then nothing ever again.
            yield b"\x47" * hls_module.PROBE_SAMPLE_BYTES
            await asyncio.sleep(3600)

        source = FakeStreamSource()
        source.open = AsyncMock(return_value=silent())
        session, process = await self._start(streamer, source, max_seconds=0.2)

        await asyncio.wait_for(session.feeder, timeout=2)
        self.assertTrue(process.stdin.closed)
        await streamer.shutdown()

    async def test_finished_feed_retires_the_session_after_a_grace_period(self):
        """Nothing more will ever be written, so the slot must not wait out
        the idle timer — but the player still gets to drain what is on disk."""
        streamer = HLSStreamer()
        key = preview_session_key(1, "55")
        calls = []
        session, _ = await self._start(
            streamer, FakeStreamSource(), key=key, on_close=lambda: calls.append(1)
        )
        await asyncio.wait_for(session.feeder, timeout=2)

        # Still servable immediately after the feed ends.
        self.assertIs(streamer.get_active(key), session)

        with patch.object(hls_module, "FEED_END_GRACE_SECONDS", 0):
            await streamer._retire_after_grace(session)

        self.assertIsNone(streamer.get_active(key))
        self.assertEqual(calls, [1], "the preview slot was not released")
        await streamer.shutdown()

    async def test_feed_stops_at_the_time_cap(self):
        streamer = HLSStreamer()

        async def endless():
            while True:
                await asyncio.sleep(0)
                yield b"\x47" * 188

        source = FakeStreamSource()
        source.open = AsyncMock(return_value=endless())
        process = FakeProcess()

        async def fake_spawn(session, cmd, stdin_pipe=False):
            return process

        with (
            patch.object(streamer, "_spawn_ffmpeg", fake_spawn),
            patch.object(
                streamer, "_probe_media", AsyncMock(return_value=("h264", "aac", "LC", None))
            ),
            patch("services.hls_streamer.post_processor") as mock_pp,
        ):
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.get_ffprobe_path.return_value = "/usr/bin/ffprobe"
            session = await streamer.get_or_create_stream(
                "preview:1:55", source, "live:None:None:None", 0.0
            )

        await asyncio.wait_for(session.feeder, timeout=2)
        self.assertTrue(process.stdin.closed, "FFmpeg input must be closed at the cap")
        await streamer.shutdown()

    async def test_live_session_reaped_sooner_than_a_file_session(self):
        streamer = HLSStreamer()
        session, _ = await self._start(streamer, FakeStreamSource())
        session.last_access = time.monotonic() - (hls_module.LIVE_IDLE_TTL_SECONDS + 5)
        async with streamer._lock:
            await streamer._reap_idle_locked()
        self.assertIsNone(streamer.get_active("preview:1:55"))
        await streamer.shutdown()


class WaitForPlaylistTests(unittest.IsolatedAsyncioTestCase):
    def _session_with_dir(self):
        directory = Path(tempfile.mkdtemp(prefix="hls-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return HLSSession(key="download:1", source_path=Path("/media/a.ts"), directory=directory)

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


LOOPBACK_SOURCE = "http://127.0.0.1:4177/api/vod/preview/source/tok3n"


class LoopbackUrlGuardTests(unittest.TestCase):
    """The URL source shape is only safe because of this check: it is what
    stops a credentialed provider URL from ever reaching FFmpeg's argv."""

    def test_loopback_urls_are_accepted(self):
        for url in [
            LOOPBACK_SOURCE,
            "http://[::1]:4177/api/vod/preview/source/t",
        ]:
            assert_loopback_url(url)

    def test_provider_urls_are_refused(self):
        for url in [
            "http://provider.example:8080/movie/user/pw/1.mkv",
            "https://127.0.0.1/x",
            "http://127.0.0.1.evil.example/x",
            # Whatever /etc/hosts says it is, so not trusted.
            "http://localhost:9/x",
            "file:///etc/passwd",
            "http://74.119.149.10/live/play/token/1",
        ]:
            with self.assertRaises(HLSStartError, msg=url):
                assert_loopback_url(url)

    def test_build_command_refuses_a_provider_url(self):
        with self.assertRaises(HLSStartError):
            HLSStreamer.build_ffmpeg_command(
                "ffmpeg", "http://provider.example/movie/u/p/1.mkv", "h264", "ac3"
            )


class VodFfmpegCommandTests(unittest.TestCase):
    """VOD preview: FFmpeg opens the loopback relay itself, so it can seek."""

    def _cmd(self, video="h264", audio="ac3", profile=None, start=0.0):
        return HLSStreamer.build_ffmpeg_command(
            "ffmpeg", LOOPBACK_SOURCE, video, audio, profile,
            start_offset=start, realtime=True,
        )

    def test_input_is_the_loopback_relay_with_no_credentials(self):
        cmd = self._cmd()
        self.assertEqual(cmd[cmd.index("-i") + 1], LOOPBACK_SOURCE)
        joined = " ".join(cmd)
        for leak in ("username=", "password=", "provider.example", "/movie/"):
            self.assertNotIn(leak, joined)

    def test_realtime_pacing_bounds_what_a_preview_writes(self):
        """Without -re FFmpeg would race through a two-hour film as fast as the
        provider serves it, filling the temp dir with unwatched segments."""
        cmd = self._cmd()
        self.assertIn("-re", cmd)
        self.assertLess(cmd.index("-re"), cmd.index("-i"))

    def test_seek_lands_before_the_input(self):
        cmd = self._cmd(start=3600.0)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "3600.000")
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))

    def test_short_segments_for_fast_startup(self):
        self.assertEqual(self._cmd()[self._cmd().index("-hls_time") + 1], "2")

    def test_ac3_audio_is_reencoded_and_h264_video_copied(self):
        cmd = self._cmd("h264", "ac3")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")


class UrlSessionTests(unittest.IsolatedAsyncioTestCase):
    """VOD preview sessions: FFmpeg reads a seekable loopback URL."""

    async def _start(self, streamer, key=None, url=LOOPBACK_SOURCE,
                     fingerprint="movie:1:mkv:0.000", start_offset=0.0,
                     probe=("h264", "ac3", None, None), max_seconds=900.0, on_close=None):
        key = key or vod_preview_session_key(1, "movie", "42")
        process = FakeProcess()

        async def fake_spawn(session, cmd, stdin_pipe=False):
            self.spawned_cmd = cmd
            return process

        with (
            patch.object(streamer, "_spawn_ffmpeg", fake_spawn),
            patch.object(streamer, "_probe_media", AsyncMock(return_value=probe)),
            patch("services.hls_streamer.post_processor") as mock_pp,
        ):
            mock_pp.get_ffmpeg_path.return_value = "/usr/bin/ffmpeg"
            mock_pp.get_ffprobe_path.return_value = "/usr/bin/ffprobe"
            session = await streamer.get_or_create_url(
                key, url, fingerprint, max_seconds,
                start_offset=start_offset, on_close=on_close,
            )
        return session, process

    async def test_session_starts_and_is_registered(self):
        streamer = HLSStreamer()
        session, _ = await self._start(streamer)
        self.assertIs(streamer.get_active(session.key), session)
        # Holds a preview slot and a provider connection, so it is reaped on
        # the same short schedule as a live preview.
        self.assertTrue(session.live)
        self.assertEqual(session.idle_ttl, hls_module.LIVE_IDLE_TTL_SECONDS)
        await streamer.shutdown()

    async def test_a_provider_url_is_refused_outright(self):
        streamer = HLSStreamer()
        with self.assertRaises(HLSStartError):
            await self._start(streamer, url="http://provider.example/movie/u/p/1.mkv")
        self.assertEqual(streamer._sessions, {})
        await streamer.shutdown()

    async def test_start_offset_reaches_ffmpeg(self):
        streamer = HLSStreamer()
        await self._start(streamer, fingerprint="movie:1:mkv:3600.000", start_offset=3600.0)
        self.assertEqual(self.spawned_cmd[self.spawned_cmd.index("-ss") + 1], "3600.000")
        await streamer.shutdown()

    async def test_seeking_to_a_new_offset_rebuilds_the_session(self):
        """Scrubbing an hour in must restart FFmpeg there, not replay the
        session that is already sitting at zero."""
        streamer = HLSStreamer()
        first, first_process = await self._start(streamer)
        second, _ = await self._start(
            streamer, fingerprint="movie:1:mkv:3600.000", start_offset=3600.0
        )
        self.assertIsNot(second, first)
        self.assertTrue(first_process.terminated)
        self.assertFalse(first.directory.exists())
        await streamer.shutdown()

    async def test_same_offset_reuses_the_session_and_returns_the_budget(self):
        streamer = HLSStreamer()
        first, _ = await self._start(streamer)
        returned = []
        second, _ = await self._start(streamer, on_close=lambda: returned.append(1))
        self.assertIs(second, first)
        self.assertEqual(returned, [1], "The caller that lost the race kept its slot.")
        await streamer.shutdown()

    async def test_no_video_track_fails_and_releases_the_slot(self):
        streamer = HLSStreamer()
        released = []
        with self.assertRaises(HLSStartError):
            await self._start(
                streamer, probe=(None, "aac", "LC", None), on_close=lambda: released.append(1)
            )
        self.assertEqual(streamer._sessions, {})
        self.assertEqual(len(released), 1)
        await streamer.shutdown()

    async def test_wall_clock_ceiling_retires_the_session(self):
        """A preview you can scrub through has no natural end, so the ceiling
        is the only thing that ever stops it."""
        streamer = HLSStreamer()
        closed = []
        session, process = await self._start(
            streamer, max_seconds=0.05, on_close=lambda: closed.append(1)
        )
        self.assertIsNotNone(session.deadline)
        await asyncio.sleep(0.2)
        self.assertIsNone(streamer.get_active(session.key))
        self.assertTrue(process.terminated)
        self.assertEqual(closed, [1])
        await streamer.shutdown()

    async def test_teardown_cancels_the_ceiling(self):
        streamer = HLSStreamer()
        session, _ = await self._start(streamer, max_seconds=600.0)
        deadline = session.deadline
        await streamer.close_session(session)
        await asyncio.sleep(0)  # let the cancellation land
        self.assertTrue(deadline.cancelled())
        await streamer.shutdown()
