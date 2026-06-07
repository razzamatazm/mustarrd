import asyncio
import json
import logging
import os
import shutil
import platform
import subprocess
import shlex
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional, Callable, List, Dict, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class OutputFormat(str, Enum):
    TS = "ts"
    MP4 = "mp4"
    MKV = "mkv"


class HardwareAccel(str, Enum):
    CPU = "cpu"
    APPLE_SILICON = "videotoolbox"  # macOS VideoToolbox (M1/M2/etc)
    NVIDIA = "nvenc"  # NVIDIA NVENC
    AMD = "amf"  # AMD AMF
    VAAPI = "vaapi"  # Linux VA-API (Intel/AMD)


# Encoder mappings for each hardware acceleration method
ENCODER_MAP = {
    HardwareAccel.CPU: {
        "h264": "libx264",
        "hevc": "libx265",
    },
    HardwareAccel.APPLE_SILICON: {
        "h264": "h264_videotoolbox",
        "hevc": "hevc_videotoolbox",
    },
    HardwareAccel.NVIDIA: {
        "h264": "h264_nvenc",
        "hevc": "hevc_nvenc",
    },
    HardwareAccel.AMD: {
        "h264": "h264_amf",
        "hevc": "hevc_amf",
    },
    HardwareAccel.VAAPI: {
        "h264": "h264_vaapi",
        "hevc": "hevc_vaapi",
    },
}


class PostProcessor:
    """Handles transcoding and commercial detection/removal."""

    VAAPI_RENDER_DEVICE = Path("/dev/dri/renderD128")
    VAAPI_SYSFS_DRM_CLASS = Path("/sys/class/drm")
    VAAPI_DEFAULT_DRIVERS_PATH = (
        "/usr/local/lib/x86_64-linux-gnu/dri:/usr/local/lib/aarch64-linux-gnu/dri:"
        "/usr/lib/x86_64-linux-gnu/dri:/usr/lib/aarch64-linux-gnu/dri"
    )

    def __init__(self):
        self._ffmpeg_path = shutil.which("ffmpeg")
        self._comskip_path = shutil.which("comskip")
        self._available_encoders: Optional[List[str]] = None

    @staticmethod
    def _is_executable(path: Optional[str]) -> bool:
        if not path:
            return False
        return os.path.isfile(path) and os.access(path, os.X_OK)

    def _resolve_ffmpeg_path(self) -> Optional[str]:
        """Resolve ffmpeg path on demand to handle PATH changes."""
        candidates = []
        env_path = os.environ.get("CATCHUP_FFMPEG_PATH") or os.environ.get("FFMPEG_PATH")
        if env_path:
            candidates.append(env_path)
        if self._ffmpeg_path:
            candidates.append(self._ffmpeg_path)

        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            candidates.append(system_ffmpeg)
        candidates.extend([
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        ])

        for path in candidates:
            if not path:
                continue
            if self._is_executable(path):
                self._ffmpeg_path = path
                return self._ffmpeg_path

        self._ffmpeg_path = None
        return None

    def _resolve_ffprobe_path(self) -> Optional[str]:
        """Resolve ffprobe path on demand for duration detection."""
        candidates = []
        env_path = os.environ.get("CATCHUP_FFPROBE_PATH") or os.environ.get("FFPROBE_PATH")
        if env_path:
            candidates.append(env_path)

        system_ffprobe = shutil.which("ffprobe")
        if system_ffprobe:
            candidates.append(system_ffprobe)

        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path:
            ffmpeg_file = Path(ffmpeg_path)
            candidates.append(str(ffmpeg_file.with_name("ffprobe")))
            candidates.append(ffmpeg_path.replace("ffmpeg", "ffprobe"))

        candidates.extend([
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
        ])

        seen = set()
        for path in candidates:
            if not path:
                continue
            if path in seen:
                continue
            seen.add(path)
            if self._is_executable(path):
                return path

        return None

    def _resolve_comskip_path(self) -> Optional[str]:
        """Resolve comskip path on demand to handle PATH changes."""
        candidates = []
        env_path = os.environ.get("CATCHUP_COMSKIP_PATH") or os.environ.get("COMSKIP_PATH")
        if env_path:
            candidates.append(env_path)
        if self._comskip_path:
            candidates.append(self._comskip_path)
        system_comskip = shutil.which("comskip")
        if system_comskip:
            candidates.append(system_comskip)
        candidates.extend([
            "/opt/homebrew/bin/comskip",
            "/usr/local/bin/comskip",
            "/usr/bin/comskip",
        ])

        for path in candidates:
            if not path:
                continue
            if self._is_executable(path):
                self._comskip_path = path
                return self._comskip_path

        self._comskip_path = None
        return None

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    def get_ffmpeg_path(self) -> Optional[str]:
        """Return resolved ffmpeg path if available."""
        return self._resolve_ffmpeg_path()

    @property
    def ffmpeg_available(self) -> bool:
        return self._resolve_ffmpeg_path() is not None

    @property
    def comskip_available(self) -> bool:
        return self._resolve_comskip_path() is not None

    def get_comskip_path(self) -> Optional[str]:
        """Return resolved comskip path if available."""
        return self._resolve_comskip_path()

    def set_comskip_path(self, path: str):
        """Set custom path to comskip binary."""
        if os.path.isfile(path):
            self._comskip_path = path

    def set_ffmpeg_path(self, path: str):
        """Set custom path to ffmpeg binary."""
        if os.path.isfile(path):
            self._ffmpeg_path = path

    def _probe_tool(
        self,
        path: Optional[str],
        args: List[str],
        allowed_exit_codes: Optional[set[int]] = None,
        timeout: float = 5.0,
    ) -> Tuple[bool, Optional[str]]:
        """Check whether a tool executable can actually run."""
        if not self._is_executable(path):
            return False, "not executable or not found"

        try:
            result = subprocess.run(
                [path, *args],
                capture_output=True,
                text=True,
                timeout=timeout
            )
        except FileNotFoundError:
            return False, "not found"
        except subprocess.TimeoutExpired:
            return False, "timed out while probing"
        except Exception as exc:
            return False, str(exc)

        if allowed_exit_codes is None:
            allowed_exit_codes = {0}

        if result.returncode not in allowed_exit_codes:
            output = (result.stderr or result.stdout or "").strip()
            output = re.sub(r"\s+", " ", output)
            if len(output) > 240:
                output = f"...{output[-240:]}"
            if output:
                return False, f"exit {result.returncode}: {output}"
            return False, f"exit {result.returncode}"
        return True, None

    def get_tool_runtime_status(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Return resolved paths plus runtime probe results for external tools."""
        ffmpeg_path = self.get_ffmpeg_path()
        ffprobe_path = self._resolve_ffprobe_path()
        comskip_path = self.get_comskip_path()

        ffmpeg_ok, ffmpeg_error = self._probe_tool(ffmpeg_path, ["-version"])
        ffprobe_ok, ffprobe_error = self._probe_tool(ffprobe_path, ["-version"])
        # Comskip commonly returns 2 for --help/usage even when functional.
        comskip_ok, comskip_error = self._probe_tool(
            comskip_path,
            ["--help"],
            allowed_exit_codes={0, 2}
        )

        return {
            "ffmpeg": {
                "path": ffmpeg_path,
                "available": ffmpeg_ok,
                "error": ffmpeg_error,
            },
            "ffprobe": {
                "path": ffprobe_path,
                "available": ffprobe_ok,
                "error": ffprobe_error,
            },
            "comskip": {
                "path": comskip_path,
                "available": comskip_ok,
                "error": comskip_error,
            },
        }

    def _get_available_encoders(self) -> List[str]:
        """Get list of available ffmpeg encoders."""
        if self._available_encoders is not None:
            return self._available_encoders

        if not self.ffmpeg_available:
            return []

        try:
            result = subprocess.run(
                [self._ffmpeg_path, "-encoders", "-hide_banner"],
                capture_output=True,
                text=True,
                timeout=10
            )
            self._available_encoders = result.stdout
            return self._available_encoders
        except Exception:
            return []

    def _read_text_file(self, path: Path) -> Optional[str]:
        try:
            return path.read_text().strip()
        except OSError:
            return None

    @lru_cache(maxsize=1)
    def _resolve_vaapi_driver(self) -> Dict[str, Optional[str]]:
        """Resolve the Linux VA-API driver to use, if any."""
        if platform.system() != "Linux":
            return {
                "enabled": False,
                "driver": None,
                "source": "unsupported",
                "device": None,
                "kernel_driver": None,
                "vendor": None,
            }

        env_driver = (os.environ.get("LIBVA_DRIVER_NAME") or "").strip()
        if env_driver:
            return {
                "enabled": True,
                "driver": env_driver,
                "source": "env",
                "device": str(self.VAAPI_RENDER_DEVICE),
                "kernel_driver": None,
                "vendor": None,
            }

        device = self.VAAPI_RENDER_DEVICE
        if not device.exists():
            return {
                "enabled": True,
                "driver": None,
                "source": "device-missing",
                "device": str(device),
                "kernel_driver": None,
                "vendor": None,
            }

        sysfs_class_entry = self.VAAPI_SYSFS_DRM_CLASS / device.name
        try:
            sysfs_base = sysfs_class_entry.resolve(strict=True)
        except OSError:
            return {
                "enabled": True,
                "driver": None,
                "source": "sysfs-unavailable",
                "device": str(device),
                "kernel_driver": None,
                "vendor": None,
            }

        driver_link = sysfs_base / "device" / "driver"
        vendor_path = sysfs_base / "device" / "vendor"
        kernel_driver = None
        vendor = self._read_text_file(vendor_path)

        try:
            kernel_driver = driver_link.resolve(strict=True).name
        except OSError:
            kernel_driver = None

        driver_map = {
            "amdgpu": "radeonsi",
            "i915": "iHD",
            "xe": "iHD",
        }
        driver = driver_map.get((kernel_driver or "").lower())
        source = "auto-detected" if driver else "auto"
        return {
            "enabled": True,
            "driver": driver,
            "source": source,
            "device": str(device),
            "kernel_driver": kernel_driver,
            "vendor": vendor,
        }

    def get_vaapi_diagnostics(self) -> Dict[str, Optional[str] | bool]:
        details = dict(self._resolve_vaapi_driver())
        details["drivers_path"] = os.environ.get("LIBVA_DRIVERS_PATH", self.VAAPI_DEFAULT_DRIVERS_PATH)
        return details

    def _build_ffmpeg_env(self, hw_accel: Optional[HardwareAccel] = None) -> Dict[str, str]:
        env = os.environ.copy()
        if platform.system() == "Linux":
            env.setdefault("LIBVA_DRIVERS_PATH", self.VAAPI_DEFAULT_DRIVERS_PATH)
            if hw_accel == HardwareAccel.VAAPI:
                details = self._resolve_vaapi_driver()
                if details.get("driver"):
                    env["LIBVA_DRIVER_NAME"] = str(details["driver"])
                else:
                    env.pop("LIBVA_DRIVER_NAME", None)
        return env

    def get_available_hardware_accels(self) -> List[Dict]:
        """Detect which hardware acceleration methods are available."""
        available = []
        encoders = self._get_available_encoders()

        # Always add CPU
        available.append({
            "id": HardwareAccel.CPU.value,
            "name": "CPU (Software)",
            "description": "Universal compatibility, slower",
            "available": True,
        })

        # Check Apple Silicon (macOS only)
        if platform.system() == "Darwin":
            if "h264_videotoolbox" in encoders:
                available.append({
                    "id": HardwareAccel.APPLE_SILICON.value,
                    "name": "Apple Silicon (VideoToolbox)",
                    "description": "Hardware acceleration for M1/M2/M3 Macs",
                    "available": True,
                })

        # Check NVIDIA
        if "h264_nvenc" in encoders:
            available.append({
                "id": HardwareAccel.NVIDIA.value,
                "name": "NVIDIA (NVENC)",
                "description": "Hardware acceleration for NVIDIA GPUs",
                "available": True,
            })

        # Check AMD
        if "h264_amf" in encoders:
            available.append({
                "id": HardwareAccel.AMD.value,
                "name": "AMD (AMF)",
                "description": "Hardware acceleration for AMD GPUs",
                "available": True,
            })

        # Check VA-API (Linux)
        if platform.system() == "Linux" and "h264_vaapi" in encoders:
            available.append({
                "id": HardwareAccel.VAAPI.value,
                "name": "VA-API (Linux)",
                "description": "Hardware acceleration via VA-API",
                "available": True,
            })

        return available

    def _get_encoder_args(
        self,
        hw_accel: HardwareAccel,
        codec: str = "h264",
        quality: str = "balanced"
    ) -> List[str]:
        """Get ffmpeg encoder arguments for the specified hardware acceleration."""
        encoder = ENCODER_MAP.get(hw_accel, {}).get(codec, "libx264")

        # Quality presets
        quality_map = {
            "fast": {"cpu_preset": "veryfast", "hw_quality": "speed"},
            "balanced": {"cpu_preset": "fast", "hw_quality": "balanced"},
            "quality": {"cpu_preset": "slow", "hw_quality": "quality"},
        }
        q = quality_map.get(quality, quality_map["balanced"])

        args = ["-c:v", encoder]

        if hw_accel == HardwareAccel.CPU:
            args.extend([
                "-preset", q["cpu_preset"],
                "-crf", "22",
            ])
        elif hw_accel == HardwareAccel.APPLE_SILICON:
            # VideoToolbox options
            args.extend([
                "-q:v", "65",  # Quality (0-100, higher is better)
            ])
        elif hw_accel == HardwareAccel.NVIDIA:
            args.extend([
                "-preset", "p4" if q["hw_quality"] == "speed" else "p5",
                "-rc", "vbr",
                "-cq", "23",
            ])
        elif hw_accel == HardwareAccel.AMD:
            args.extend([
                "-quality", q["hw_quality"],
                "-rc", "vbr_latency",
            ])
        elif hw_accel == HardwareAccel.VAAPI:
            args.extend([
                "-vf", "format=nv12,hwupload",
                "-qp", "23",
            ])

        return args

    def _preferred_video_codec(self, hw_accel: HardwareAccel) -> str:
        """Choose default encoder codec per hardware backend."""
        if hw_accel == HardwareAccel.APPLE_SILICON:
            # Match desired macOS VideoToolbox defaults.
            return "hevc"
        return "h264"

    def _parse_ffmpeg_time(self, key: str, value: str) -> Optional[float]:
        """Parse ffmpeg progress time values into seconds."""
        try:
            if key in ("out_time_ms", "out_time_us"):
                # ffmpeg reports microseconds for both out_time_ms and out_time_us
                micros = int(value)
                return micros / 1_000_000
            if key == "out_time":
                # Format: HH:MM:SS.micro
                parts = value.split(":")
                if len(parts) != 3:
                    return None
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
                return (hours * 3600) + (minutes * 60) + seconds
        except ValueError:
            return None
        return None

    async def _notify_progress(self, progress_callback: Optional[Callable[[float], None]], progress: float):
        """Safely call a progress callback which may be sync or async."""
        if not progress_callback:
            return
        result = progress_callback(progress)
        if asyncio.iscoroutine(result):
            await result

    async def _notify_log(self, log_callback: Optional[Callable[[str], None]], message: str):
        """Safely call a log callback which may be sync or async."""
        if not log_callback:
            return
        result = log_callback(message)
        if asyncio.iscoroutine(result):
            await result

    def _write_ffmpeg_log(self, base_path: str, tag: str, stderr: bytes) -> Optional[str]:
        """Write ffmpeg stderr to a log file next to the media file."""
        if not stderr:
            return None
        try:
            path = Path(base_path).with_suffix(f".{tag}.ffmpeg.log")
            path.write_bytes(stderr)
            return str(path)
        except Exception:
            return None

    async def _run_ffmpeg_with_progress(
        self,
        cmd: List[str],
        duration: float,
        progress_callback: Optional[Callable[[float], None]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> tuple[int, bytes]:
        """Run ffmpeg and emit progress updates using -progress output."""
        runtime_env = env or self._build_ffmpeg_env()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=runtime_env
            )
        except FileNotFoundError as e:
            raise Exception(f"ffmpeg not found at {cmd[0]}") from e

        stderr_chunks: List[bytes] = []

        async def read_stderr():
            while True:
                chunk = await process.stderr.readline()
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        async def read_stdout():
            last_progress = -1.0
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if not text or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                seconds = self._parse_ffmpeg_time(key, value)
                if seconds is None or duration <= 0:
                    continue
                progress = min(100.0, (seconds / duration) * 100.0)
                if progress_callback and (progress - last_progress >= 0.5 or progress >= 100):
                    last_progress = progress
                    await self._notify_progress(progress_callback, progress)

        try:
            await asyncio.gather(read_stdout(), read_stderr())
            await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        if process.returncode == 0:
            await self._notify_progress(progress_callback, 100.0)

        return process.returncode or 0, b"".join(stderr_chunks)

    def _with_error_tolerant_flags(self, cmd: List[str]) -> List[str]:
        """Add ffmpeg flags to continue through corrupt data when possible."""
        if not cmd:
            return cmd
        if "-fflags" in cmd or "-err_detect" in cmd:
            return cmd
        return [cmd[0], "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err", *cmd[1:]]

    def _prepend_vaapi_env(self, cmd: List[str]) -> List[str]:
        """Run ffmpeg with an explicit LIBVA_DRIVER_NAME prefix for VA-API."""
        if platform.system() != "Linux":
            return cmd
        if not cmd:
            return cmd
        details = self._resolve_vaapi_driver()
        drivers_path = os.environ.get("LIBVA_DRIVERS_PATH", self.VAAPI_DEFAULT_DRIVERS_PATH)
        env_cmd = ["/usr/bin/env", f"LIBVA_DRIVERS_PATH={drivers_path}"]
        if details.get("driver"):
            env_cmd.append(f"LIBVA_DRIVER_NAME={details['driver']}")
        return [*env_cmd, *cmd]

    def _escape_concat_path(self, path: Path) -> str:
        text = str(path)
        text = text.replace("\\", "\\\\")
        return text.replace("'", "'\\''")

    @staticmethod
    def _primary_av_map_args() -> list:
        """Map primary video plus first audio track (if present)."""
        return ["-map", "0:v:0", "-map", "0:a:0?"]

    @staticmethod
    def _parse_ratio(value: Optional[str]) -> float:
        if not value or value in ("0/0", "N/A"):
            return 0.0
        if "/" in value:
            try:
                num, den = value.split("/", 1)
                den_f = float(den)
                if den_f == 0:
                    return 0.0
                return float(num) / den_f
            except (ValueError, ZeroDivisionError):
                return 0.0
        try:
            return float(value)
        except ValueError:
            return 0.0

    @staticmethod
    def _as_int(value: Optional[object]) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _select_best_av_map_args(
        self,
        input_path: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> list:
        """
        Select the best video/audio stream indexes using ffprobe.
        Falls back to primary streams when probing is unavailable.
        """
        ffprobe = self._resolve_ffprobe_path()
        if not ffprobe:
            return self._primary_av_map_args()

        cmd = [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_entries",
            "stream=index,codec_type,width,height,avg_frame_rate,bit_rate,channels,disposition",
            str(input_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            return self._primary_av_map_args()

        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            stderr_text = (stderr or b"").decode(errors="ignore").strip()
            if stderr_text:
                await self._notify_log(log_callback, f"ffprobe stream probe failed: {stderr_text}")
            return self._primary_av_map_args()

        try:
            payload = json.loads((stdout or b"{}").decode(errors="ignore"))
        except json.JSONDecodeError:
            return self._primary_av_map_args()

        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        if not streams:
            return self._primary_av_map_args()

        video_candidates = []
        audio_candidates = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            stream_index = self._as_int(stream.get("index"))
            stream_type = stream.get("codec_type")
            disposition = stream.get("disposition") or {}
            is_default = 1 if isinstance(disposition, dict) and disposition.get("default") else 0

            if stream_type == "video":
                width = self._as_int(stream.get("width"))
                height = self._as_int(stream.get("height"))
                fps = self._parse_ratio(stream.get("avg_frame_rate"))
                bitrate = self._as_int(stream.get("bit_rate"))
                pixels = max(0, width * height)
                score = (is_default, pixels, fps, bitrate, -stream_index)
                video_candidates.append((score, stream_index, width, height, fps, bitrate))
            elif stream_type == "audio":
                channels = self._as_int(stream.get("channels"))
                bitrate = self._as_int(stream.get("bit_rate"))
                score = (is_default, channels, bitrate, -stream_index)
                audio_candidates.append((score, stream_index, channels, bitrate))

        if not video_candidates:
            return self._primary_av_map_args()

        video_candidates.sort(reverse=True)
        selected_video = video_candidates[0]
        video_index = selected_video[1]

        if audio_candidates:
            audio_candidates.sort(reverse=True)
            selected_audio = audio_candidates[0]
            audio_index = selected_audio[1]
            map_args = ["-map", f"0:{video_index}", "-map", f"0:{audio_index}?"]
        else:
            map_args = ["-map", f"0:{video_index}"]

        if len(video_candidates) > 1 or len(audio_candidates) > 1:
            await self._notify_log(
                log_callback,
                f"Multiple streams detected; selected video stream 0:{video_index}"
                + (f" and audio stream 0:{audio_index}." if audio_candidates else " (no audio stream selected).")
            )

        return map_args

    async def transcode(
        self,
        input_path: str,
        output_format: OutputFormat,
        hw_accel: HardwareAccel = HardwareAccel.CPU,
        quality: str = "balanced",
        progress_callback: Optional[Callable[[float], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        remove_original: bool = False,
        remux_only: bool = False
    ) -> str:
        """
        Transcode a video file to a different format.

        Args:
            input_path: Path to input file
            output_format: Target format (mp4, mkv, ts)
            hw_accel: Hardware acceleration method
            quality: Quality preset (fast, balanced, quality)
            progress_callback: Optional callback for progress updates
            remove_original: Whether to delete the original file after transcoding

        Returns:
            Path to the transcoded file
        """
        input_file = Path(input_path)
        output_path = input_file.with_suffix(f".{output_format.value}")

        # Same input and output format is always a no-op regardless of hw_accel.
        # Without this guard, non-CPU hw_accel falls through and ffmpeg writes to the
        # same path as its input, truncating the recording to 0 bytes before reading.
        if input_file.suffix.lower() == f".{output_format.value}":
            return str(input_path)

        if not self.ffmpeg_available:
            raise Exception("ffmpeg not found. Please install ffmpeg.")

        selected_map_args = await self._select_best_av_map_args(input_path, log_callback)

        # Build ffmpeg command
        cmd = [self._ffmpeg_path]
        if hw_accel == HardwareAccel.VAAPI:
            cmd.extend(["-vaapi_device", "/dev/dri/renderD128"])
        cmd.extend([
            "-i", str(input_path),
            "-y",  # Overwrite output
        ])
        cmd = self._with_error_tolerant_flags(cmd)

        if output_format in [OutputFormat.MP4, OutputFormat.MKV]:
            if remux_only:
                if input_file.suffix.lower() == ".ts":
                    cmd.extend(selected_map_args)
                else:
                    cmd.extend(["-map", "0"])
                cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
            else:
                if input_file.suffix.lower() == ".ts":
                    cmd.extend(selected_map_args)
                # Add video encoder args
                cmd.extend(self._get_encoder_args(
                    hw_accel,
                    self._preferred_video_codec(hw_accel),
                    quality
                ))
                # Audio - AAC for compatibility
                cmd.extend(["-c:a", "aac"])
        else:
            # TS - copy streams
            cmd.extend(["-c", "copy"])

        cmd.extend([
            "-progress", "pipe:1",
            "-nostats",
            str(output_path)
        ])

        await self._notify_log(
            log_callback,
            f"ffmpeg cmd: {' '.join(shlex.quote(str(c)) for c in cmd)}"
        )
        ffmpeg_env = self._build_ffmpeg_env(hw_accel)
        if hw_accel == HardwareAccel.VAAPI:
            vaapi = self.get_vaapi_diagnostics()
            cmd = self._prepend_vaapi_env(cmd)
            await self._notify_log(
                log_callback,
                "ffmpeg env: "
                f"LIBVA_DRIVER_NAME={ffmpeg_env.get('LIBVA_DRIVER_NAME', 'auto/unset')} "
                f"LIBVA_DRIVERS_PATH={ffmpeg_env.get('LIBVA_DRIVERS_PATH', self.VAAPI_DEFAULT_DRIVERS_PATH)} "
                f"(source={vaapi.get('source')}, kernel_driver={vaapi.get('kernel_driver') or 'unknown'})"
            )
        elif platform.system() == "Linux":
            await self._notify_log(
                log_callback,
                f"ffmpeg env: LIBVA_DRIVERS_PATH={ffmpeg_env.get('LIBVA_DRIVERS_PATH', self.VAAPI_DEFAULT_DRIVERS_PATH)}"
            )

        # Run ffmpeg with progress
        duration = await self._get_duration(input_path, log_callback=log_callback)
        returncode, stderr = await self._run_ffmpeg_with_progress(
            cmd,
            duration,
            progress_callback,
            env=ffmpeg_env,
        )
        if returncode != 0:
            if remux_only and output_format in [OutputFormat.MP4, OutputFormat.MKV]:
                await self._notify_log(
                    log_callback,
                    "ffmpeg remux failed; retrying with full transcode (video+audio)."
                )
                if output_path.exists():
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                retry_cmd = [self._ffmpeg_path]
                if hw_accel == HardwareAccel.VAAPI:
                    retry_cmd.extend(["-vaapi_device", "/dev/dri/renderD128"])
                retry_cmd.extend([
                    "-i", str(input_path),
                    "-y",
                ])
                retry_cmd = self._with_error_tolerant_flags(retry_cmd)
                if input_file.suffix.lower() == ".ts":
                    retry_cmd.extend(selected_map_args)
                else:
                    retry_cmd.extend(["-map", "0"])
                retry_cmd.extend(self._get_encoder_args(
                    hw_accel,
                    self._preferred_video_codec(hw_accel),
                    quality
                ))
                retry_cmd.extend(["-c:a", "aac", "-avoid_negative_ts", "make_zero"])
                retry_cmd.extend([
                    "-progress", "pipe:1",
                    "-nostats",
                    str(output_path)
                ])
                await self._notify_log(
                    log_callback,
                    f"ffmpeg retry cmd: {' '.join(shlex.quote(str(c)) for c in retry_cmd)}"
                )
                if hw_accel == HardwareAccel.VAAPI:
                    retry_cmd = self._prepend_vaapi_env(retry_cmd)
                returncode, stderr = await self._run_ffmpeg_with_progress(
                    retry_cmd,
                    duration,
                    progress_callback,
                    env=ffmpeg_env,
                )
                if returncode == 0:
                    return str(output_path)

            log_path = self._write_ffmpeg_log(str(input_path), "transcode", stderr)
            if log_path:
                await self._notify_log(log_callback, f"ffmpeg log saved: {log_path}")
            raise Exception(f"ffmpeg failed: {stderr.decode(errors='ignore')}")

        # Remove original if requested
        if remove_original and output_path.exists():
            os.remove(input_path)

        return str(output_path)

    async def detect_commercials(
        self,
        input_path: str,
        ini_path: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[float], None]] = None
    ) -> Optional[str]:
        """
        Run Comskip to detect commercials and generate EDL file.

        Args:
            input_path: Path to video file
            ini_path: Optional path to comskip.ini config file
            progress_callback: Optional callback for progress updates

        Returns:
            Path to the EDL file, or None if no commercials detected
        """
        if not self.comskip_available:
            raise Exception("Comskip not found. Please install Comskip from https://github.com/erikkaashoek/Comskip")

        input_file = Path(input_path)
        output_dir = input_file.parent

        async def run_comskip_once(source_path: str, progress_prefix: str = "Comskip") -> tuple[int, str]:
            cmd = [self._comskip_path]
            if ini_path and os.path.isfile(ini_path):
                cmd.extend(["--ini", ini_path])
            cmd.extend([
                "--output", str(output_dir),
                str(source_path)
            ])

            await self._notify_log(
                log_callback,
                f"{progress_prefix} cmd: {' '.join(str(c) for c in cmd)}"
            )

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            percent_pattern = re.compile(r"(\d{1,3})%{1,2}")
            last_percent: Optional[int] = None
            stdout_output = ""
            stderr_output = ""
            max_output_chars = 24000

            def _append_output(existing: str, addition: str) -> str:
                combined = existing + addition
                if len(combined) > max_output_chars:
                    return combined[-max_output_chars:]
                return combined

            async def read_stream(stream: asyncio.StreamReader, is_stderr: bool):
                nonlocal last_percent, stdout_output, stderr_output
                tail = ""
                while True:
                    chunk = await stream.read(1024)
                    if not chunk:
                        break
                    decoded = chunk.decode(errors="ignore")
                    text = tail + decoded
                    if is_stderr:
                        stderr_output = _append_output(stderr_output, decoded)
                    else:
                        stdout_output = _append_output(stdout_output, decoded)
                    max_percent = None
                    for match in percent_pattern.finditer(text):
                        try:
                            percent = int(match.group(1))
                        except ValueError:
                            continue
                        if 0 <= percent <= 100:
                            if max_percent is None or percent > max_percent:
                                max_percent = percent
                    if max_percent is not None:
                        if last_percent is None or max_percent > last_percent:
                            last_percent = max_percent
                            await self._notify_log(log_callback, f"{progress_prefix} progress: {max_percent}%")
                            await self._notify_progress(progress_callback, float(max_percent))
                    tail = text[-32:]

            try:
                await asyncio.gather(
                    read_stream(process.stdout, False),
                    read_stream(process.stderr, True)
                )
                await process.wait()
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise

            returncode = process.returncode if process.returncode is not None else -1
            combined_output = (stderr_output.strip() or stdout_output.strip())
            return returncode, combined_output

        def excerpt(text: str, max_len: int) -> str:
            value = re.sub(r"\s+", " ", text).strip()
            if len(value) > max_len:
                return f"...{value[-max_len:]}"
            return value

        returncode, combined_output = await run_comskip_once(input_path)
        active_input_file = input_file
        temp_probe_file: Optional[Path] = None

        if returncode != 0 and input_file.suffix.lower() == ".ts" and self.ffmpeg_available:
            failed_excerpt = excerpt(combined_output, 600)
            if failed_excerpt:
                await self._notify_log(log_callback, f"Comskip failed (exit {returncode}): {failed_excerpt}")
            await self._notify_log(
                log_callback,
                "Comskip failed on TS input; retrying on normalized intermediate file."
            )
            selected_map_args = await self._select_best_av_map_args(input_path, log_callback)
            video_map = selected_map_args[1] if len(selected_map_args) >= 2 else "0:v:0"
            temp_probe_file = input_file.with_stem(f"{input_file.stem}_comskip_input").with_suffix(".mkv")

            prep_cmd = [self._ffmpeg_path, "-i", str(input_file), "-y"]
            prep_cmd = self._with_error_tolerant_flags(prep_cmd)
            prep_cmd.extend(selected_map_args)
            prep_cmd.extend([
                "-c:v", "copy",
                "-c:a", "aac",
                "-af", "aresample=async=1:first_pts=0",
                "-avoid_negative_ts", "make_zero",
                str(temp_probe_file)
            ])
            await self._notify_log(
                log_callback,
                f"Comskip prep cmd: {' '.join(shlex.quote(str(c)) for c in prep_cmd)}"
            )
            prep_process = await asyncio.create_subprocess_exec(
                *prep_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, prep_stderr = await prep_process.communicate()

            if prep_process.returncode != 0:
                await self._notify_log(
                    log_callback,
                    "Comskip prep with audio failed; retrying prep with video-only stream."
                )
                video_only_cmd = [self._ffmpeg_path, "-i", str(input_file), "-y"]
                video_only_cmd = self._with_error_tolerant_flags(video_only_cmd)
                video_only_cmd.extend([
                    "-map", video_map,
                    "-an",
                    "-c:v", "copy",
                    "-avoid_negative_ts", "make_zero",
                    str(temp_probe_file)
                ])
                await self._notify_log(
                    log_callback,
                    f"Comskip prep cmd (video-only): {' '.join(shlex.quote(str(c)) for c in video_only_cmd)}"
                )
                prep_process = await asyncio.create_subprocess_exec(
                    *video_only_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, prep_stderr = await prep_process.communicate()

            if prep_process.returncode == 0:
                active_input_file = temp_probe_file
                returncode, combined_output = await run_comskip_once(
                    str(temp_probe_file),
                    progress_prefix="Comskip retry"
                )
            else:
                prep_excerpt = excerpt((prep_stderr or b"").decode(errors="ignore"), 400)
                if prep_excerpt:
                    await self._notify_log(log_callback, f"Comskip prep failed: {prep_excerpt}")

        try:
            if returncode != 0:
                failed_excerpt = excerpt(combined_output, 600)
                if failed_excerpt:
                    await self._notify_log(
                        log_callback,
                        f"Comskip failed (exit {returncode}): {failed_excerpt}"
                    )
                raise Exception(
                    f"Comskip exited with code {returncode}"
                    + (f": {failed_excerpt}" if failed_excerpt else "")
                )

            edl_path = self._find_edl_for_input(active_input_file, output_dir)
            if edl_path:
                return edl_path

            if combined_output:
                await self._notify_log(
                    log_callback,
                    f"Comskip finished without EDL output: {excerpt(combined_output, 400)}"
                )
            else:
                await self._notify_log(log_callback, "Comskip finished without EDL output.")
            return None
        finally:
            if temp_probe_file and temp_probe_file.exists():
                try:
                    os.remove(temp_probe_file)
                except Exception:
                    pass

    async def remove_commercials(
        self,
        input_path: str,
        edl_path: str,
        output_format: OutputFormat = OutputFormat.MP4,
        hw_accel: HardwareAccel = HardwareAccel.CPU,
        remove_original: bool = False,
        progress_callback: Optional[Callable[[float], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        remux_only: bool = False
    ) -> str:
        """
        Remove commercials from video using EDL file.

        Args:
            input_path: Path to video file
            edl_path: Path to EDL file from Comskip
            output_format: Output format
            hw_accel: Hardware acceleration method
            remove_original: Whether to delete original after processing

        Returns:
            Path to the commercial-free video
        """
        if not self.ffmpeg_available:
            raise Exception("ffmpeg not found. Please install ffmpeg.")

        # Parse EDL file to get commercial segments
        segments = self._parse_edl(edl_path)

        if not segments:
            # No commercials, just transcode if needed
            output = await self.transcode(
                input_path,
                output_format,
                hw_accel,
                progress_callback=progress_callback,
                log_callback=log_callback,
                remove_original=remove_original,
                remux_only=remux_only
            )
            self._cleanup_comskip_outputs(input_path, edl_path)
            return output

        input_file = Path(input_path)
        output_path = input_file.with_suffix(f".{output_format.value}")
        same_path_output = output_path.resolve() == input_file.resolve()
        work_output_path = output_path
        if same_path_output:
            work_output_path = input_file.with_stem(
                f"{input_file.stem}_postproc_tmp"
            ).with_suffix(f".{output_format.value}")
            if work_output_path.exists():
                try:
                    work_output_path.unlink()
                except Exception:
                    pass

        # Get video duration
        duration = await self._get_duration(input_path, log_callback=log_callback)
        await self._notify_log(
            log_callback,
            f"Commercial removal: duration={duration:.3f}s"
        )
        if duration <= 0:
            await self._notify_log(
                log_callback,
                "Commercial removal: ffprobe duration unavailable; last segment will run to EOF."
            )

        # Build keep segments (inverse of commercial segments)
        keep_segments = self._invert_segments(segments, duration)
        await self._notify_log(
            log_callback,
            f"Commercial removal: keep segments={keep_segments}"
        )

        if not keep_segments:
            output = await self.transcode(
                input_path,
                output_format,
                hw_accel,
                progress_callback=progress_callback,
                log_callback=log_callback,
                remove_original=remove_original,
                remux_only=remux_only
            )
            self._cleanup_comskip_outputs(input_path, edl_path)
            return output

        # Create concat file for ffmpeg
        concat_file = input_file.with_suffix(".concat.txt")
        temp_files = []
        selected_map_args = await self._select_best_av_map_args(input_path, log_callback)

        try:
            await self._notify_log(
                log_callback,
                f"Commercial removal: extracting {len(keep_segments)} keep segments with ffmpeg copy."
            )
            # Extract each segment
            segment_count = len(keep_segments)
            for i, (start, end) in enumerate(keep_segments):
                temp_path = input_file.with_stem(f"{input_file.stem}_seg{i}").with_suffix(".ts")
                temp_files.append(temp_path)

                omit_to = False
                if duration <= 0 or i == segment_count - 1:
                    if duration <= 0 or end >= (duration - 0.01) or end <= start:
                        omit_to = True
                if end <= start and not omit_to:
                    raise Exception(
                        f"Invalid keep segment (end <= start): start={start}, end={end}, "
                        f"duration={duration}"
                    )

                cmd = [self._ffmpeg_path, "-i", str(input_path), "-ss", str(start)]
                if not omit_to:
                    cmd.extend(["-to", str(end)])
                cmd.extend(selected_map_args)
                cmd.extend(["-c", "copy", "-y", str(temp_path)])
                cmd = self._with_error_tolerant_flags(cmd)

                await self._notify_log(
                    log_callback,
                    f"ffmpeg segment cmd: {' '.join(shlex.quote(str(c)) for c in cmd)}"
                )

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, seg_stderr = await process.communicate()
                if process.returncode != 0:
                    log_path = self._write_ffmpeg_log(str(input_path), f"seg{i}", seg_stderr or b"")
                    if log_path:
                        await self._notify_log(log_callback, f"ffmpeg log saved: {log_path}")
                    raise Exception(
                        f"ffmpeg segment extraction failed: {seg_stderr.decode(errors='ignore')}"
                    )
                if progress_callback and segment_count > 0:
                    await self._notify_progress(progress_callback, (i + 1) / segment_count * 40.0)

            # Write concat file
            with open(concat_file, "w") as f:
                for temp_path in temp_files:
                    f.write(f"file '{self._escape_concat_path(temp_path)}'\n")

            # Concat and encode
            cmd = [self._ffmpeg_path]
            if hw_accel == HardwareAccel.VAAPI:
                cmd.extend(["-vaapi_device", "/dev/dri/renderD128"])
            cmd.extend([
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
            ])
            cmd = self._with_error_tolerant_flags(cmd)

            if output_format in [OutputFormat.MP4, OutputFormat.MKV]:
                if remux_only:
                    cmd.extend(self._primary_av_map_args())
                    cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
                else:
                    cmd.extend(self._primary_av_map_args())
                    cmd.extend(self._get_encoder_args(
                        hw_accel,
                        self._preferred_video_codec(hw_accel)
                    ))
                    cmd.extend(["-c:a", "aac"])
            else:
                cmd.extend(["-c", "copy"])

            cmd.extend(["-progress", "pipe:1", "-nostats", "-y", str(work_output_path)])

            kept_duration = sum(max(0.0, end - start) for start, end in keep_segments)
            async def mapped_callback(p: float):
                await self._notify_progress(progress_callback, 40.0 + (p * 0.6))
            await self._notify_log(
                log_callback,
                f"ffmpeg concat cmd: {' '.join(shlex.quote(str(c)) for c in cmd)}"
            )
            ffmpeg_env = self._build_ffmpeg_env(hw_accel)
            if hw_accel == HardwareAccel.VAAPI:
                vaapi = self.get_vaapi_diagnostics()
                cmd = self._prepend_vaapi_env(cmd)
                await self._notify_log(
                    log_callback,
                    "ffmpeg env: "
                    f"LIBVA_DRIVER_NAME={ffmpeg_env.get('LIBVA_DRIVER_NAME', 'auto/unset')} "
                    f"LIBVA_DRIVERS_PATH={ffmpeg_env.get('LIBVA_DRIVERS_PATH', self.VAAPI_DEFAULT_DRIVERS_PATH)} "
                    f"(source={vaapi.get('source')}, kernel_driver={vaapi.get('kernel_driver') or 'unknown'})"
                )
            returncode, stderr = await self._run_ffmpeg_with_progress(
                cmd,
                kept_duration,
                progress_callback=mapped_callback if progress_callback else None,
                env=ffmpeg_env,
            )

            if returncode != 0:
                if remux_only and output_format in [OutputFormat.MP4, OutputFormat.MKV]:
                    await self._notify_log(
                        log_callback,
                        "ffmpeg concat remux failed; retrying with full transcode (video+audio)."
                    )
                    if work_output_path.exists():
                        try:
                            work_output_path.unlink()
                        except Exception:
                            pass
                    retry_cmd = [self._ffmpeg_path]
                    if hw_accel == HardwareAccel.VAAPI:
                        retry_cmd.extend(["-vaapi_device", "/dev/dri/renderD128"])
                    retry_cmd.extend([
                        "-f", "concat",
                        "-safe", "0",
                        "-i", str(concat_file),
                    ])
                    retry_cmd = self._with_error_tolerant_flags(retry_cmd)
                    retry_cmd.extend(self._primary_av_map_args())
                    retry_cmd.extend(self._get_encoder_args(
                        hw_accel,
                        self._preferred_video_codec(hw_accel)
                    ))
                    retry_cmd.extend(["-c:a", "aac", "-avoid_negative_ts", "make_zero"])
                    retry_cmd.extend(["-progress", "pipe:1", "-nostats", "-y", str(work_output_path)])
                    await self._notify_log(
                        log_callback,
                        f"ffmpeg concat retry cmd: {' '.join(shlex.quote(str(c)) for c in retry_cmd)}"
                    )
                    if hw_accel == HardwareAccel.VAAPI:
                        retry_cmd = self._prepend_vaapi_env(retry_cmd)
                    returncode, stderr = await self._run_ffmpeg_with_progress(
                        retry_cmd,
                        kept_duration,
                        progress_callback=mapped_callback if progress_callback else None,
                        env=ffmpeg_env,
                    )

                if returncode != 0:
                    log_path = self._write_ffmpeg_log(str(input_path), "concat", stderr)
                    if log_path:
                        await self._notify_log(log_callback, f"ffmpeg log saved: {log_path}")
                    raise Exception(f"ffmpeg concat failed: {stderr.decode(errors='ignore')}")

        finally:
            # Cleanup temp files
            for temp_path in temp_files:
                if temp_path.exists():
                    os.remove(temp_path)
            if concat_file.exists():
                os.remove(concat_file)

        self._cleanup_comskip_outputs(input_path, edl_path)

        if same_path_output:
            if not work_output_path.exists():
                raise Exception("Commercial removal output missing before replace.")
            os.replace(str(work_output_path), str(output_path))
        elif remove_original:
            os.remove(input_path)

        return str(output_path)

    def _cleanup_comskip_outputs(self, input_path: str, edl_path: str) -> None:
        """Remove comskip output artifacts."""
        try:
            input_file = Path(input_path)
            candidates = [
                input_file.with_suffix(".edl"),
                input_file.with_suffix(".txt"),
                input_file.with_suffix(".log"),
                input_file.with_suffix(".logo"),
                input_file.with_suffix(".csv"),
                input_file.with_suffix(".vdr"),
            ]
            if edl_path:
                edl_file = Path(edl_path)
                candidates.append(edl_file)
                if edl_file.stem != input_file.stem:
                    for suffix in [".txt", ".log", ".logo", ".csv", ".vdr"]:
                        candidates.append(edl_file.with_suffix(suffix))
            for path in candidates:
                if path.exists():
                    os.remove(path)
        except Exception:
            pass

    def _find_edl_for_input(self, input_file: Path, output_dir: Path) -> Optional[str]:
        """Locate EDL output that corresponds to a specific input media file."""
        edl_candidates = [
            input_file.with_suffix(".edl"),
            output_dir / f"{input_file.name}.edl",
            output_dir / f"{input_file.stem}.edl",
        ]
        for edl_path in edl_candidates:
            if edl_path.exists():
                return str(edl_path)

        for pattern in (f"{input_file.stem}*.edl", f"{input_file.name}*.edl"):
            matches = sorted(output_dir.glob(pattern))
            if matches:
                return str(matches[0])

        return None

    def _parse_edl(self, edl_path: str) -> list:
        """Parse EDL file and return list of (start, end, type) tuples."""
        segments = []
        with open(edl_path, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    try:
                        start = float(parts[0])
                        end = float(parts[1])
                        seg_type = int(parts[2])
                    except (ValueError, IndexError):
                        logger.warning("Skipping malformed EDL line: %r", line.strip())
                        continue
                    # Type 0 = cut, 3 = commercial
                    if seg_type in [0, 3] and end > start:
                        segments.append((start, end, seg_type))
        return segments

    def _invert_segments(self, commercial_segments: list, duration: float) -> list:
        """Convert commercial segments to keep segments."""
        if not commercial_segments:
            return [(0, duration)]

        keep_segments = []
        current_pos = 0

        for start, end, _ in sorted(commercial_segments):
            if end <= start:
                continue
            if start > current_pos:
                keep_segments.append((current_pos, start))
            current_pos = end

        if current_pos < duration:
            keep_segments.append((current_pos, duration))

        return keep_segments

    async def _get_duration(
        self,
        input_path: str,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> float:
        """Get video duration using ffprobe."""
        ffprobe = self._resolve_ffprobe_path()

        if not ffprobe:
            await self._notify_log(
                log_callback,
                "ffprobe not found; duration unavailable. Progress may be limited."
            )
            return 0

        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path)
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            await self._notify_log(
                log_callback,
                f"ffprobe not found at {ffprobe}; duration unavailable."
            )
            return 0

        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            stderr_text = stderr.decode(errors="ignore").strip()
            if stderr_text:
                await self._notify_log(log_callback, f"ffprobe failed: {stderr_text}")
            return 0
        try:
            return float(stdout.decode().strip())
        except ValueError:
            await self._notify_log(log_callback, "ffprobe returned invalid duration.")
            return 0


# Global instance
post_processor = PostProcessor()
