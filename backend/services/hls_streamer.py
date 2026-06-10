"""
On-the-fly HLS repackaging for browser playback of completed downloads.

Browsers only natively play MP4 (H.264/AAC). Recordings can be raw MPEG-TS,
MKV, or carry AC-3/MP2 audio that no browser decodes via MSE. This service
spawns one FFmpeg per watched download that repackages the file into an
fMP4 HLS stream (video copied when the codec is browser-decodable, audio
transcoded to AAC unless it already is AAC), served from a per-session temp
directory. Sessions are reaped after an idle TTL.
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
from typing import Dict, Optional

from services.post_processor import post_processor

logger = logging.getLogger(__name__)

# Asset names a client may request from a session directory. Anything else
# is rejected before touching the filesystem.
# \Z (not $) so names with a trailing newline can't sneak past validation.
HLS_ASSET_PATTERN = re.compile(r"\A(playlist\.m3u8|init\.mp4|seg\d{5}\.m4s)\Z")

# Video codecs browsers can decode via MSE/native HLS; copied as-is.
COPYABLE_VIDEO_CODECS = {"h264", "hevc"}

IDLE_TTL_SECONDS = 120
MAX_SESSIONS = 4
PLAYLIST_WAIT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 60.0
REAPER_INTERVAL_SECONDS = 30


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
    download_id: int
    source_path: Path
    directory: Path
    process: Optional[asyncio.subprocess.Process] = None
    last_access: float = field(default_factory=time.monotonic)
    failed_reason: Optional[str] = None

    @property
    def playlist_path(self) -> Path:
        return self.directory / "playlist.m3u8"


class HLSStreamer:
    def __init__(self):
        self._sessions: Dict[int, HLSSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None
        # Per-process root: shutdown() removes the whole tree, and a shared
        # name would let one instance (e.g. desktop app) wipe another's
        # (e.g. dev server) live sessions on the same machine.
        self._root_dir = Path(tempfile.gettempdir()) / f"mustarrd-hls-{os.getpid()}"

    # ------------------------------------------------------------------ probe

    async def _probe_codecs(self, ffprobe: str, source: Path) -> tuple[Optional[str], Optional[str]]:
        """Return (video_codec, audio_codec) of the first streams, or Nones."""
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
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
    def _parse_probe_output(raw: str) -> tuple[Optional[str], Optional[str]]:
        try:
            streams = json.loads(raw).get("streams", [])
        except (json.JSONDecodeError, AttributeError):
            raise HLSStartError("The recording could not be probed")
        video_codec = None
        audio_codec = None
        for stream in streams:
            codec_type = stream.get("codec_type")
            if codec_type == "video" and video_codec is None:
                video_codec = stream.get("codec_name")
            elif codec_type == "audio" and audio_codec is None:
                audio_codec = stream.get("codec_name")
        return video_codec, audio_codec

    # ----------------------------------------------------------------- ffmpeg

    @staticmethod
    def build_ffmpeg_command(
        ffmpeg: str,
        source: Path,
        video_codec: Optional[str],
        audio_codec: Optional[str],
    ) -> list[str]:
        cmd = [
            ffmpeg,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(source),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-sn",
            "-dn",
        ]
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
        if audio_codec == "aac":
            # ADTS-framed AAC (MPEG-TS sources) must be converted to ASC for
            # fMP4; the filter passes non-ADTS packets through untouched.
            cmd.extend(["-c:a", "copy", "-bsf:a", "aac_adtstoasc"])
        elif audio_codec is not None:
            cmd.extend(["-c:a", "aac", "-b:a", "160k", "-ac", "2"])
        cmd.extend([
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "event",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", "seg%05d.m4s",
            "playlist.m3u8",
        ])
        return cmd

    # --------------------------------------------------------------- sessions

    def get_active(self, download_id: int) -> Optional[HLSSession]:
        session = self._sessions.get(download_id)
        if session and not session.failed_reason:
            return session
        return None

    @staticmethod
    def touch(session: HLSSession) -> None:
        session.last_access = time.monotonic()

    async def get_or_create(self, download_id: int, source_path: Path) -> HLSSession:
        async with self._lock:
            existing = self._sessions.get(download_id)
            if existing:
                process = existing.process
                crashed = (
                    process is not None
                    and process.returncode is not None
                    and process.returncode != 0
                )
                if existing.source_path == source_path and not existing.failed_reason and not crashed:
                    self.touch(existing)
                    return existing
                # Source changed (re-download), FFmpeg crashed, or a previous
                # attempt failed: rebuild from scratch.
                await self._destroy_locked(existing)

            await self._reap_idle_locked()
            if len(self._sessions) >= MAX_SESSIONS:
                raise HLSLimitError(
                    "Too many active playback sessions. Close another player and try again."
                )

            ffmpeg = post_processor.get_ffmpeg_path()
            ffprobe = post_processor.get_ffprobe_path()
            if not ffmpeg or not ffprobe:
                raise HLSUnavailableError("FFmpeg is required for browser playback but was not found")

            video_codec, audio_codec = await self._probe_codecs(ffprobe, source_path)
            if video_codec is None:
                raise HLSStartError("The recording has no video stream")

            self._root_dir.mkdir(parents=True, exist_ok=True)
            session_dir = Path(tempfile.mkdtemp(prefix=f"dl{download_id}-", dir=self._root_dir))
            session = HLSSession(
                download_id=download_id,
                source_path=source_path,
                directory=session_dir,
            )

            cmd = self.build_ffmpeg_command(ffmpeg, source_path, video_codec, audio_codec)
            # stderr goes to a log file in the session dir: no pipe to drain,
            # and the tail is available for error reporting.
            stderr_path = session_dir / "ffmpeg.log"
            try:
                with stderr_path.open("wb") as stderr_file:
                    session.process = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(session_dir),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=stderr_file,
                    )
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

            self._sessions[download_id] = session
            self._ensure_reaper()
            logger.info(
                "HLS session started download_id=%s video=%s audio=%s dir=%s",
                download_id, video_codec, audio_codec, session_dir,
            )
            return session

    async def wait_for_playlist(self, session: HLSSession) -> None:
        """Block until the playlist references at least one segment."""
        deadline = time.monotonic() + PLAYLIST_WAIT_SECONDS
        while time.monotonic() < deadline:
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
        for download_id in list(self._sessions):
            session = self._sessions[download_id]
            if now - session.last_access > IDLE_TTL_SECONDS:
                logger.info("Reaping idle HLS session download_id=%s", download_id)
                await self._destroy_locked(session)

    async def _destroy_locked(self, session: HLSSession) -> None:
        self._sessions.pop(session.download_id, None)
        process = session.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        shutil.rmtree(session.directory, ignore_errors=True)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        async with self._lock:
            for download_id in list(self._sessions):
                await self._destroy_locked(self._sessions[download_id])
        shutil.rmtree(self._root_dir, ignore_errors=True)


hls_streamer = HLSStreamer()
