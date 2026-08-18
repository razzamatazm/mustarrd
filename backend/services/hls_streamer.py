"""
On-the-fly HLS repackaging for browser playback.

Browsers only natively play MP4 (H.264/AAC). Recordings can be raw MPEG-TS,
MKV, or carry AC-3/MP2 audio that no browser decodes via MSE, and live
provider streams have exactly the same problem. This service spawns one
FFmpeg per watched source that repackages it into an fMP4 HLS stream (video
copied when the codec is browser-decodable, audio transcoded to AAC unless it
already is AAC-LC), served from a per-session temp directory. Sessions are
reaped after an idle TTL.

Three source shapes are supported, all keyed by an opaque session key:

- a finished file on disk (download playback), read by FFmpeg directly;
- a byte stream the caller supplies (Converted preview of a live or catchup
  provider URL), fed to FFmpeg over stdin;
- a loopback URL (VOD preview), which FFmpeg opens itself.

The invariant behind all three: a provider URL — which embeds account
credentials — never reaches FFmpeg's argv, where `ps` would expose it to every
user on the host. The stream shape upholds it by having the caller own the
provider connection, so FFmpeg only ever sees `pipe:0`. The URL shape upholds
it by accepting *only* `http://127.0.0.1:<port>/…`, checked here rather than
trusted from the caller: that URL is a relay this process serves, and carries
an opaque token in place of credentials. A pipe cannot be seeked, which is why
the VOD preview — the one source a viewer needs to scrub through — needs the
URL shape at all.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Optional, Protocol, Union
from urllib.parse import urlsplit

from services.post_processor import post_processor

logger = logging.getLogger(__name__)

# Asset names a client may request from a session directory. Anything else
# is rejected before touching the filesystem.
# \Z (not $) so names with a trailing newline can't sneak past validation.
HLS_ASSET_PATTERN = re.compile(r"\A(playlist\.m3u8|init\.mp4|seg\d{5}\.m4s)\Z")

# Video codecs browsers can decode via MSE/native HLS; copied as-is.
COPYABLE_VIDEO_CODECS = {"h264", "hevc"}

# FFmpeg input spec for a stream source. Never a URL: see the module docstring.
PIPE_INPUT = "pipe:0"

# The only hosts a URL source may name. Anything else could be a provider URL
# with credentials in it, which is the one thing argv must never carry.
# Numeric only, and deliberately: "localhost" is whatever /etc/hosts says it is,
# and it is the same set the relay endpoint checks its callers against.
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

IDLE_TTL_SECONDS = 120
# Live sessions hold a shared preview slot and an FFmpeg process that never
# ends on its own, so they are reaped far sooner than file sessions. A player
# on an event playlist keeps polling, which keeps the session touched. This
# only covers a viewer who walked away: a session whose feed has ended is
# retired directly, on FEED_END_GRACE_SECONDS.
LIVE_IDLE_TTL_SECONDS = 45
# How long a finished live session stays servable so the player can drain the
# segments already on disk before the directory goes away.
FEED_END_GRACE_SECONDS = 15
MAX_SESSIONS = 4
PLAYLIST_WAIT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 60.0
REAPER_INTERVAL_SECONDS = 30

# How much of a live stream to buffer before probing it. Enough for ffprobe to
# see both a video and an audio stream in a multiplexed TS without adding a
# noticeable wait to opening a preview.
PROBE_SAMPLE_BYTES = 2 * 1024 * 1024
PROBE_SAMPLE_SECONDS = 15.0
STREAM_WRITE_CHUNK = 64 * 1024


class StreamSource(Protocol):
    """A byte source FFmpeg reads over stdin instead of opening itself."""

    async def open(self) -> AsyncIterator[bytes]:
        """Connect and return an async iterator over the stream's bytes."""

    async def close(self) -> None:
        """Release the connection. Must tolerate being called twice."""


def assert_loopback_url(url: str) -> None:
    """Refuse anything FFmpeg must not be handed as an argument.

    Enforced here, at the point argv is built, rather than left to callers:
    this is the check that keeps the module's credentials-never-reach-argv
    invariant true no matter who adds the next caller.
    """
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise HLSStartError("Only a loopback source URL may be handed to FFmpeg")


def download_session_key(download_id: int) -> str:
    return f"download:{download_id}"


def preview_session_key(account_id: int, channel_id: str) -> str:
    return f"preview:{account_id}:{channel_id}"


def vod_preview_session_key(account_id: int, kind: str, item_id: str) -> str:
    return f"vodpreview:{account_id}:{kind}:{item_id}"


class HLSError(Exception):
    """Base error for HLS streaming problems."""


class HLSUnavailableError(HLSError):
    """FFmpeg/ffprobe is not available on this system."""


class HLSLimitError(HLSError):
    """Too many concurrent playback sessions."""


class HLSStartError(HLSError):
    """FFmpeg failed to produce a playable stream."""


@dataclass
class HLSSession:
    key: str
    directory: Path
    # File sources only; a stream session reads from stdin instead.
    source_path: Optional[Path] = None
    start_offset: float = 0.0
    live: bool = False
    # Identifies what this session is rendering, so a request for something
    # else under the same key rebuilds instead of serving the wrong stream.
    # Never carries credentials: it is derived from the request parameters.
    fingerprint: str = ""
    process: Optional[asyncio.subprocess.Process] = None
    feeder: Optional[asyncio.Task] = None
    # Wall-clock ceiling on a URL session. A stream session gets its cap from
    # the feeder's deadline instead; a URL session has no feeder to hang it on,
    # because FFmpeg pulls the bytes itself.
    deadline: Optional[asyncio.Task] = None
    # Called exactly once when the session is destroyed, so the caller can
    # release whatever budget it reserved (e.g. a preview slot).
    on_close: Optional[Callable[[], None]] = None
    last_access: float = field(default_factory=time.monotonic)
    failed_reason: Optional[str] = None
    destroyed: bool = False

    @property
    def playlist_path(self) -> Path:
        return self.directory / "playlist.m3u8"

    @property
    def idle_ttl(self) -> float:
        return LIVE_IDLE_TTL_SECONDS if self.live else IDLE_TTL_SECONDS


class HLSStreamer:
    def __init__(self):
        self._sessions: Dict[str, HLSSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None
        self._pending_closes: set = set()
        # Per-process root: shutdown() removes the whole tree, and a shared
        # name would let one instance (e.g. desktop app) wipe another's
        # (e.g. dev server) live sessions on the same machine.
        self._root_dir = Path(tempfile.gettempdir()) / f"mustarrd-hls-{os.getpid()}"

    # ------------------------------------------------------------------ probe

    async def _probe_media(
        self, ffprobe: str, source: Union[Path, str]
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
        """Return (video_codec, audio_codec, audio_profile, duration) of the
        first streams, or Nones for whatever is missing."""
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise HLSStartError("Probing the recording timed out")
        if process.returncode != 0:
            raise HLSStartError("The recording could not be probed")
        return self._parse_probe_output(stdout.decode("utf-8", errors="replace"))

    @staticmethod
    def _parse_probe_output(
        raw: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
        try:
            parsed = json.loads(raw)
            streams = parsed.get("streams", [])
        except (json.JSONDecodeError, AttributeError):
            raise HLSStartError("The recording could not be probed")
        video_codec = None
        audio_codec = None
        audio_profile = None
        for stream in streams:
            codec_type = stream.get("codec_type")
            if codec_type == "video" and video_codec is None:
                video_codec = stream.get("codec_name")
            elif codec_type == "audio" and audio_codec is None:
                audio_codec = stream.get("codec_name")
                audio_profile = stream.get("profile")
        duration = None
        try:
            duration = float(parsed.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            pass
        return video_codec, audio_codec, audio_profile, duration

    async def probe_duration(self, source: Union[Path, str]) -> Optional[float]:
        """Best-effort duration in seconds of a file or loopback URL, or None."""
        if not isinstance(source, Path):
            assert_loopback_url(str(source))
        ffprobe = post_processor.get_ffprobe_path()
        if not ffprobe:
            return None
        try:
            _, _, _, duration = await self._probe_media(ffprobe, source)
        except HLSError:
            return None
        return duration

    # ----------------------------------------------------------------- ffmpeg

    @staticmethod
    def build_ffmpeg_command(
        ffmpeg: str,
        source: Union[Path, str],
        video_codec: Optional[str],
        audio_codec: Optional[str],
        audio_profile: Optional[str] = None,
        start_offset: float = 0.0,
        live: bool = False,
        realtime: bool = False,
    ) -> list[str]:
        if str(source) != PIPE_INPUT and not isinstance(source, Path):
            # A URL in argv is only ever allowed to be our own relay.
            assert_loopback_url(str(source))
        from_pipe = str(source) == PIPE_INPUT
        # Both flavours of preview want a fast first frame and short segments;
        # only a finished file on disk is worth muxing at six seconds a chunk.
        quick_start = live or realtime
        cmd = [ffmpeg, "-y"]
        if not from_pipe:
            # Reading the input from stdin means we cannot also close it.
            cmd.append("-nostdin")
        cmd.extend(["-hide_banner", "-loglevel", "error"])
        if quick_start:
            # The caller already probed the head of the stream, so FFmpeg does
            # not need to spend its default 5s analysing before it muxes.
            cmd.extend(["-analyzeduration", "2000000", "-probesize", "2000000"])
        if start_offset > 0:
            # Input-side seek: lands on the keyframe at/before the target so
            # copied video starts decodable.
            cmd.extend(["-ss", f"{start_offset:.3f}"])
        if realtime:
            # Pace the read at playback speed. Without it FFmpeg would tear
            # through a two-hour film as fast as the provider serves it,
            # filling the temp directory with segments nobody watches and
            # burning the account's connection allowance to do it.
            cmd.append("-re")
        cmd.extend([
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-sn",
            "-dn",
        ])
        if video_codec in COPYABLE_VIDEO_CODECS:
            cmd.extend(["-c:v", "copy"])
            if video_codec == "hevc":
                # Safari only plays HEVC in fMP4 when the track is tagged hvc1.
                cmd.extend(["-tag:v", "hvc1"])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-force_key_frames", "expr:gte(t,n_forced*6)",
            ])
        if audio_codec == "aac" and audio_profile == "LC":
            # Only AAC-LC is copied: Firefox's MSE rejects any AAC codec
            # string other than mp4a.40.2/.5/.29, and provider streams often
            # carry HE-AAC or a mislabeled Main profile (mp4a.40.1) that
            # throws bufferAddCodecError. Re-encoding guarantees LC.
            # ADTS-framed AAC (MPEG-TS sources) must be converted to ASC for
            # fMP4; the filter passes non-ADTS packets through untouched.
            cmd.extend(["-c:a", "copy", "-bsf:a", "aac_adtstoasc"])
        elif audio_codec is not None:
            cmd.extend(["-c:a", "aac", "-b:a", "160k", "-ac", "2"])
        cmd.extend([
            "-f", "hls",
            # A preview is a ten-second interaction, so a six-second first
            # segment would be most of it. Video is copied, so short segments
            # cost essentially nothing.
            "-hls_time", "2" if quick_start else "6",
            "-hls_playlist_type", "event",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "seg%05d.m4s",
            "playlist.m3u8",
        ])
        return cmd

    # --------------------------------------------------------------- sessions

    def get_active(self, key: str) -> Optional[HLSSession]:
        session = self._sessions.get(key)
        if session and not session.failed_reason:
            return session
        return None

    @staticmethod
    def touch(session: HLSSession) -> None:
        session.last_access = time.monotonic()

    @staticmethod
    def _is_stale(session: HLSSession, fingerprint: str) -> bool:
        process = session.process
        exited = process is not None and process.returncode is not None
        if session.failed_reason or session.fingerprint != fingerprint:
            return True
        # A finished live session has an ENDLIST playlist that would replay a
        # stale window, so any exit retires it. A file session that finished
        # cleanly is still perfectly playable.
        return exited and (session.live or process.returncode != 0)

    def _make_session_dir(self, key: str) -> Path:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        prefix = re.sub(r"[^A-Za-z0-9]+", "-", key)[:40] + "-"
        return Path(tempfile.mkdtemp(prefix=prefix, dir=self._root_dir))

    async def _spawn_ffmpeg(
        self, session: HLSSession, cmd: list[str], stdin_pipe: bool = False
    ) -> asyncio.subprocess.Process:
        # stderr goes to a log file in the session dir: no pipe to drain,
        # and the tail is available for error reporting.
        stderr_path = session.directory / "ffmpeg.log"
        with stderr_path.open("wb") as stderr_file:
            return await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(session.directory),
                stdin=asyncio.subprocess.PIPE if stdin_pipe else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=stderr_file,
            )

    async def get_or_create_file(
        self, key: str, source_path: Path, start_offset: float = 0.0
    ) -> HLSSession:
        """Repackage a file on disk. FFmpeg opens the file itself."""
        fingerprint = f"file:{source_path}:{start_offset:.3f}"
        async with self._lock:
            reused = await self._reuse_or_clear_locked(key, fingerprint)
            if reused is not None:
                return reused

            ffmpeg, ffprobe = await self._reserve_slot_locked()

            video_codec, audio_codec, audio_profile, _ = await self._probe_media(
                ffprobe, source_path
            )
            if video_codec is None:
                raise HLSStartError("The recording has no video stream")

            session_dir = self._make_session_dir(key)
            session = HLSSession(
                key=key,
                source_path=source_path,
                directory=session_dir,
                start_offset=start_offset,
                fingerprint=fingerprint,
            )

            cmd = self.build_ffmpeg_command(
                ffmpeg, source_path, video_codec, audio_codec, audio_profile, start_offset
            )
            try:
                session.process = await self._spawn_ffmpeg(session, cmd)
            except OSError as exc:
                shutil.rmtree(session_dir, ignore_errors=True)
                raise HLSStartError(f"FFmpeg could not be started: {exc}")
            except BaseException:
                # CancelledError (client gone mid-spawn) or anything else:
                # the session was never registered, so nothing would ever
                # reap the temp dir or the just-spawned FFmpeg.
                if session.process is not None and session.process.returncode is None:
                    session.process.kill()
                shutil.rmtree(session_dir, ignore_errors=True)
                raise

            self._sessions[key] = session
            self._ensure_reaper()
            logger.info(
                "HLS session started key=%s video=%s audio=%s profile=%s start=%.1f dir=%s",
                key, video_codec, audio_codec, audio_profile, start_offset, session_dir,
            )
            return session

    async def _reuse_or_clear_locked(
        self, key: str, fingerprint: str
    ) -> Optional[HLSSession]:
        """Return the session under `key` if it still renders `fingerprint`,
        otherwise retire it so the caller can build a fresh one."""
        existing = self._sessions.get(key)
        if existing is None:
            return None
        if not self._is_stale(existing, fingerprint):
            self.touch(existing)
            return existing
        # Source changed (re-download), seek to a new offset, FFmpeg crashed,
        # or a previous attempt failed: rebuild from scratch.
        await self._destroy_locked(existing)
        return None

    async def _reserve_slot_locked(self) -> tuple[str, str]:
        """Make room for one more session and return the FFmpeg tool paths."""
        await self._reap_idle_locked()
        if len(self._sessions) >= MAX_SESSIONS:
            raise HLSLimitError(
                "Too many active playback sessions. Close another player and try again."
            )
        ffmpeg = post_processor.get_ffmpeg_path()
        ffprobe = post_processor.get_ffprobe_path()
        if not ffmpeg or not ffprobe:
            raise HLSUnavailableError("FFmpeg is required for browser playback but was not found")
        return ffmpeg, ffprobe

    # ------------------------------------------------------------- url source

    async def get_or_create_url(
        self,
        key: str,
        url: str,
        fingerprint: str,
        max_seconds: float,
        start_offset: float = 0.0,
        on_close: Optional[Callable[[], None]] = None,
    ) -> HLSSession:
        """Repackage a loopback URL (VOD preview), which FFmpeg opens itself.

        Unlike the stream shape, FFmpeg can seek this source, so `start_offset`
        genuinely starts mid-file rather than reading forward to get there.
        `max_seconds` is a wall-clock ceiling on the whole session — a preview
        you can scrub through has no natural end, so it needs one.

        `on_close` follows the same contract as `get_or_create_stream`: called
        exactly once, including when this call loses the race to an equivalent
        session that already exists.
        """
        assert_loopback_url(url)
        async with self._lock:
            reused = await self._reuse_or_clear_locked(key, fingerprint)
            if reused is not None:
                if on_close is not None:
                    on_close()
                return reused

            ffmpeg, ffprobe = await self._reserve_slot_locked()

            session = HLSSession(
                key=key,
                directory=self._make_session_dir(key),
                start_offset=start_offset,
                # Reaped on the live schedule: this session holds a preview
                # slot and a provider connection, and FFmpeg paced by -re will
                # not end on its own inside a viewer's attention span.
                live=True,
                fingerprint=fingerprint,
                on_close=on_close,
            )
            # Registered before probing, for the same reason the stream shape
            # does it: probing reaches the provider and can take seconds, and
            # holding the lock for that would stall every other player.
            self._sessions[key] = session
            self._ensure_reaper()

        try:
            await self._start_url_session(session, url, ffmpeg, ffprobe, max_seconds)
        except BaseException as exc:
            session.failed_reason = str(exc) or "The stream could not be prepared"
            async with self._lock:
                await self._destroy_locked(session)
            raise
        return session

    async def _start_url_session(
        self,
        session: HLSSession,
        url: str,
        ffmpeg: str,
        ffprobe: str,
        max_seconds: float,
    ) -> None:
        video_codec, audio_codec, audio_profile, _ = await self._probe_media(ffprobe, url)
        if video_codec is None:
            raise HLSStartError("This title has no video track")

        cmd = self.build_ffmpeg_command(
            ffmpeg,
            url,
            video_codec,
            audio_codec,
            audio_profile,
            start_offset=session.start_offset,
            realtime=True,
        )
        try:
            session.process = await self._spawn_ffmpeg(session, cmd)
        except OSError as exc:
            raise HLSStartError(f"FFmpeg could not be started: {exc}")

        session.deadline = asyncio.create_task(self._stop_at_deadline(session, max_seconds))
        logger.info(
            "VOD preview started key=%s video=%s audio=%s profile=%s start=%.1f dir=%s",
            session.key, video_codec, audio_codec, audio_profile,
            session.start_offset, session.directory,
        )

    async def _stop_at_deadline(self, session: HLSSession, max_seconds: float) -> None:
        """Retire a URL session once its wall-clock ceiling passes."""
        await asyncio.sleep(max_seconds)
        # Cleared before teardown: _destroy_locked cancels this task, and
        # cancelling the task that is running it would abort teardown halfway.
        session.deadline = None
        logger.info("VOD preview hit its time cap key=%s", session.key)
        await self.close_session(session)

    # ---------------------------------------------------------- stream source

    async def get_or_create_stream(
        self,
        key: str,
        source: StreamSource,
        fingerprint: str,
        max_seconds: float,
        on_close: Optional[Callable[[], None]] = None,
    ) -> HLSSession:
        """Repackage a caller-supplied byte stream (Converted preview).

        The returned session may still be starting up; callers wait on
        `wait_for_playlist`. `on_close` is invoked exactly once either way:
        when the session this call created is torn down, or immediately if an
        equivalent session already existed — so a caller that loses the race
        gets its budget back without having to detect the race itself.
        """
        async with self._lock:
            reused = await self._reuse_or_clear_locked(key, fingerprint)
            if reused is not None:
                # This call reserved budget for a session it did not create.
                if on_close is not None:
                    on_close()
                return reused

            ffmpeg, ffprobe = await self._reserve_slot_locked()

            session = HLSSession(
                key=key,
                directory=self._make_session_dir(key),
                live=True,
                fingerprint=fingerprint,
                on_close=on_close,
            )
            # Registered before the provider connection is opened: probing a
            # live stream takes seconds, and holding the lock for that long
            # would stall every other player. A concurrent request for the
            # same key now finds this session and waits on its playlist.
            self._sessions[key] = session
            self._ensure_reaper()

        try:
            await self._start_stream_session(session, source, ffmpeg, ffprobe, max_seconds)
        except BaseException as exc:
            session.failed_reason = str(exc) or "The stream could not be prepared"
            async with self._lock:
                await self._destroy_locked(session)
            raise
        return session

    async def _start_stream_session(
        self,
        session: HLSSession,
        source: StreamSource,
        ffmpeg: str,
        ffprobe: str,
        max_seconds: float,
    ) -> None:
        chunks = await source.open()
        # Until the feeder task owns the connection, every failure path here
        # has to hand it back — otherwise the provider socket leaks.
        try:
            sample_path = session.directory / "probe.ts"
            try:
                head = await asyncio.wait_for(
                    self._sample_stream_head(chunks, sample_path), PROBE_SAMPLE_SECONDS
                )
            except asyncio.TimeoutError:
                raise HLSStartError("The provider stream stalled before it could be inspected")
            if not head:
                raise HLSStartError("The provider sent no data for this stream")

            video_codec, audio_codec, audio_profile, _ = await self._probe_media(
                ffprobe, sample_path
            )
            sample_path.unlink(missing_ok=True)
            if video_codec is None:
                raise HLSStartError("The stream has no video track")

            cmd = self.build_ffmpeg_command(
                ffmpeg, PIPE_INPUT, video_codec, audio_codec, audio_profile, live=True
            )
            try:
                session.process = await self._spawn_ffmpeg(session, cmd, stdin_pipe=True)
            except OSError as exc:
                raise HLSStartError(f"FFmpeg could not be started: {exc}")
        except BaseException:
            self._detach(source.close())
            raise

        session.feeder = asyncio.create_task(
            self._feed_ffmpeg(session, source, chunks, head, max_seconds)
        )
        logger.info(
            "Converted preview started key=%s video=%s audio=%s profile=%s dir=%s",
            session.key, video_codec, audio_codec, audio_profile, session.directory,
        )

    @staticmethod
    async def _sample_stream_head(chunks: AsyncIterator[bytes], sample_path: Path) -> bytes:
        """Buffer the first bytes of the stream so ffprobe can inspect a file.

        The sample is returned as well as written: it is the start of the
        stream FFmpeg must receive, so it has to be replayed into stdin.
        """
        buffered = bytearray()
        with sample_path.open("wb") as handle:
            async for chunk in chunks:
                buffered.extend(chunk)
                handle.write(chunk)
                if len(buffered) >= PROBE_SAMPLE_BYTES:
                    break
        return bytes(buffered)

    async def _feed_ffmpeg(
        self,
        session: HLSSession,
        source: StreamSource,
        chunks: AsyncIterator[bytes],
        head: bytes,
        max_seconds: float,
    ) -> None:
        """Pump provider bytes into FFmpeg's stdin until the preview cap."""
        process = session.process
        stdin = process.stdin if process is not None else None
        if stdin is None:
            return
        try:
            # The cap is a wall-clock deadline on the whole feed, not a check
            # between chunks: a provider that goes quiet must not be able to
            # hold the stream open past it by simply sending nothing.
            await asyncio.wait_for(self._pump(stdin, chunks, head), max_seconds)
        except asyncio.TimeoutError:
            logger.info("Converted preview hit its time cap key=%s", session.key)
        except (BrokenPipeError, ConnectionResetError):
            # FFmpeg exited (session torn down, or it gave up on the input).
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Converted preview feed failed key=%s", session.key)
        finally:
            try:
                stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
            # Detached: this finally can run with a CancelledError pending
            # (session teardown), and the first await would re-raise before
            # the provider connection was released.
            self._detach(source.close())
            # The feed is over, so this session will never produce another
            # segment. Retire it on a short grace period — long enough for the
            # player to drain what was already written, short enough that the
            # preview slot is not held on an idle timer's schedule.
            self._detach(self._retire_after_grace(session))

    @staticmethod
    async def _pump(stdin, chunks: AsyncIterator[bytes], head: bytes) -> None:
        # The probe sample is the start of the stream, so it is replayed
        # rather than dropped.
        for offset in range(0, len(head), STREAM_WRITE_CHUNK):
            stdin.write(head[offset:offset + STREAM_WRITE_CHUNK])
            await stdin.drain()
        async for chunk in chunks:
            stdin.write(chunk)
            await stdin.drain()

    async def _retire_after_grace(self, session: HLSSession) -> None:
        await asyncio.sleep(FEED_END_GRACE_SECONDS)
        await self.close_session(session)

    def _detach(self, coro) -> None:
        """Run a cleanup coroutine without awaiting it, keeping a strong ref.

        The event loop only holds weak references to tasks, so an unanchored
        cleanup task could be collected before it runs.
        """
        task = asyncio.ensure_future(coro)
        self._pending_closes.add(task)
        task.add_done_callback(self._pending_closes.discard)

    async def wait_for_playlist(self, session: HLSSession) -> None:
        """Block until the playlist references at least one segment."""
        deadline = time.monotonic() + PLAYLIST_WAIT_SECONDS
        while time.monotonic() < deadline:
            # A concurrent request may already have given up on this session.
            if session.failed_reason:
                raise HLSStartError(session.failed_reason)
            # Check for process death first: a crashed FFmpeg can leave a
            # truncated playlist behind that must not be served as success.
            process = session.process
            if process is not None and process.returncode is not None and process.returncode != 0:
                session.failed_reason = self._read_ffmpeg_error(session)
                raise HLSStartError(session.failed_reason)
            playlist = session.playlist_path
            if playlist.is_file():
                try:
                    if ".m4s" in playlist.read_text(encoding="utf-8", errors="replace"):
                        return
                except OSError:
                    pass
            await asyncio.sleep(0.2)
        session.failed_reason = "Timed out preparing the stream"
        raise HLSStartError(session.failed_reason)

    @staticmethod
    def _read_ffmpeg_error(session: HLSSession) -> str:
        log_path = session.directory / "ffmpeg.log"
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if tail:
                return f"FFmpeg failed: {tail[-1][:300]}"
        except OSError:
            pass
        return "FFmpeg failed to prepare the stream"

    # ---------------------------------------------------------------- cleanup

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                async with self._lock:
                    await self._reap_idle_locked()
                    if not self._sessions:
                        return
        except asyncio.CancelledError:
            raise

    async def _reap_idle_locked(self) -> None:
        now = time.monotonic()
        for key in list(self._sessions):
            session = self._sessions[key]
            if now - session.last_access > session.idle_ttl:
                logger.info("Reaping idle HLS session key=%s", key)
                await self._destroy_locked(session)

    async def _destroy_locked(self, session: HLSSession) -> None:
        # Idempotent: a failed start can race the reaper, and releasing the
        # caller's budget twice would hand out a preview slot that is in use.
        if session.destroyed:
            return
        session.destroyed = True
        self._sessions.pop(session.key, None)
        feeder = session.feeder
        if feeder is not None and not feeder.done():
            feeder.cancel()
        deadline = session.deadline
        if deadline is not None and not deadline.done():
            deadline.cancel()
        process = session.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        shutil.rmtree(session.directory, ignore_errors=True)
        if session.on_close is not None:
            session.on_close()

    async def close_session(self, session: HLSSession) -> None:
        """Tear down this exact session, if it is still the one under its key.

        Identity-checked, not key-checked: a failed request must not kill the
        replacement session a later request has already started under the same
        key, which would 404 a healthy player mid-playback.
        """
        async with self._lock:
            if self._sessions.get(session.key) is session:
                await self._destroy_locked(session)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        async with self._lock:
            for key in list(self._sessions):
                await self._destroy_locked(self._sessions[key])
        for task in list(self._pending_closes):
            task.cancel()
        self._pending_closes.clear()
        shutil.rmtree(self._root_dir, ignore_errors=True)


hls_streamer = HLSStreamer()
