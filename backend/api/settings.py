from fastapi import APIRouter, Depends
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Literal, Optional
import asyncio
import errno
import logging
import os
import re
import tempfile

from auth import require_admin, require_authenticated, AuthContext
from database import get_session
from config import (
    ensure_config_files,
    settings as app_settings,
    is_docker_env,
    is_desktop_env,
    legacy_desktop_download_folder,
    legacy_desktop_completed_folder,
)
from models import AppSettings, XtreamAccount
from services.comskip_ini import ComskipIniError, validate_comskip_ini_path
from services.download_manager import download_manager
from services.epg_service import epg_service
from services.post_processor import post_processor, normalize_comskip_hw_decode_mode
from schedule_timing import (
    MAX_SCHEDULED_DOWNLOAD_DELAY_MINUTES,
    resolve_scheduled_download_delay_minutes,
)


router = APIRouter()

logger = logging.getLogger(__name__)



def _probe_folder_writable(path: str) -> dict:
    """Check that a recording folder exists and is writable.

    The write test creates and immediately deletes a hidden temp file in the
    folder, so a folder that merely *lists* fine but sits on a disconnected
    mount, a read-only filesystem, or a permission-restricted share is reported
    with a concrete reason instead of failing silently at download time.
    """
    info = {"path": path, "exists": False, "writable": False, "error": None}
    if not path:
        info["error"] = "No folder is configured."
        return info
    if not os.path.isdir(os.path.expanduser(path)):
        info["error"] = (
            "Folder does not exist. If it is on an external drive or network "
            "share, check that the mount is connected."
        )
        return info
    info["exists"] = True
    try:
        fd, probe_path = tempfile.mkstemp(
            prefix=".mustarrd-write-test-", dir=os.path.expanduser(path)
        )
        os.close(fd)
        os.unlink(probe_path)
        info["writable"] = True
    except PermissionError:
        info["error"] = "No permission to write to this folder."
    except OSError as exc:
        if exc.errno == errno.EROFS:
            info["error"] = "The folder is on a read-only filesystem."
        elif exc.errno == errno.ENOSPC:
            info["error"] = "The disk is full."
        else:
            info["error"] = f"Write test failed: {exc.strerror or exc}"
    return info


def _paths_match(path_a: Optional[str], path_b: Optional[str]) -> bool:
    if not path_a or not path_b:
        return False
    norm_a = os.path.realpath(os.path.abspath(os.path.expanduser(path_a)))
    norm_b = os.path.realpath(os.path.abspath(os.path.expanduser(path_b)))
    return norm_a == norm_b


class SettingsUpdate(BaseModel):
    download_folder: Optional[str] = None
    completed_folder: Optional[str] = None
    tv_template: Optional[str] = None
    movie_template: Optional[str] = None
    sports_template: Optional[str] = None
    default_template: Optional[str] = None
    max_concurrent_downloads: Optional[int] = Field(default=None, ge=1, le=50)
    max_concurrent_post_processing: Optional[int] = Field(default=None, ge=1, le=20)
    min_free_space_gb: Optional[int] = Field(default=None, ge=1)
    default_pre_padding_minutes: Optional[int] = Field(default=None, ge=0, le=120)
    default_post_padding_minutes: Optional[int] = Field(default=None, ge=0, le=120)
    scheduled_download_delay_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        le=MAX_SCHEDULED_DOWNLOAD_DELAY_MINUTES,
    )
    default_account_id: Optional[int] = None
    # Post-processing
    transcode_enabled: Optional[bool] = None
    transcode_format: Optional[Literal["ts", "mp4", "mkv"]] = None
    hw_accel: Optional[Literal["cpu", "videotoolbox", "nvenc", "amf", "vaapi"]] = None
    vaapi_render_device: Optional[str] = Field(default=None, max_length=255)

    @field_validator("vaapi_render_device")
    @classmethod
    def _validate_render_device(cls, value: Optional[str]) -> Optional[str]:
        """Empty clears the override; anything else must be a DRM render node."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return ""
        if not re.fullmatch(r"/dev/dri/renderD\d+", cleaned):
            raise ValueError(
                "Render device must look like /dev/dri/renderD128"
            )
        return cleaned
    delete_original_after_transcode: Optional[bool] = None
    remux_only: Optional[bool] = None
    integrity_check_enabled: Optional[bool] = None
    write_nfo_files: Optional[bool] = None
    comskip_enabled: Optional[bool] = None
    comskip_cut: Optional[bool] = None
    comskip_path: Optional[str] = None
    comskip_ini_path: Optional[str] = None
    comskip_use_custom_ini: Optional[bool] = None
    comskip_custom_ini_path: Optional[str] = None
    comskip_detect_method: Optional[int] = Field(default=None, ge=0, le=255)
    comskip_max_commercialbreak: Optional[int] = Field(default=None, ge=0)
    comskip_min_commercialbreak: Optional[int] = Field(default=None, ge=0)
    comskip_max_commercial_size: Optional[int] = Field(default=None, ge=0)
    comskip_min_commercial_size: Optional[int] = Field(default=None, ge=0)
    comskip_always_keep_first_seconds: Optional[int] = Field(default=None, ge=0)
    comskip_always_keep_last_seconds: Optional[int] = Field(default=None, ge=0)
    comskip_remove_before: Optional[int] = Field(default=None, ge=0)
    comskip_remove_after: Optional[int] = Field(default=None, ge=0)
    comskip_connect_blocks_with_logo: Optional[bool] = None
    comskip_dynamic_ticker_tape: Optional[bool] = None
    # Clamped to 1..16 in update_settings rather than rejected.
    comskip_thread_count: Optional[int] = None
    # Anything unrecognised is coerced to "none" in update_settings rather than
    # rejected, so a stale client can never 500 the settings save.
    comskip_hw_decode_mode: Optional[str] = None
    epg_offset_minutes: Optional[int] = None
    show_future_programs: Optional[bool] = None
    launch_on_startup: Optional[bool] = None
    auto_retry_failed_downloads: Optional[bool] = None


NON_NULLABLE_FIELDS = {
    "download_folder",
    "completed_folder",
    "tv_template",
    "movie_template",
    "sports_template",
    "default_template",
    "max_concurrent_downloads",
    "max_concurrent_post_processing",
    "min_free_space_gb",
    "default_pre_padding_minutes",
    "default_post_padding_minutes",
    "scheduled_download_delay_minutes",
    "transcode_enabled",
    "transcode_format",
    "hw_accel",
    "vaapi_render_device",
    "delete_original_after_transcode",
    "remux_only",
    "integrity_check_enabled",
    "write_nfo_files",
    "comskip_enabled",
    "comskip_cut",
    "comskip_use_custom_ini",
    "comskip_detect_method",
    "comskip_max_commercialbreak",
    "comskip_min_commercialbreak",
    "comskip_max_commercial_size",
    "comskip_min_commercial_size",
    "comskip_always_keep_first_seconds",
    "comskip_always_keep_last_seconds",
    "comskip_remove_before",
    "comskip_remove_after",
    "comskip_connect_blocks_with_logo",
    "comskip_dynamic_ticker_tape",
    "comskip_thread_count",
    "comskip_hw_decode_mode",
    "epg_offset_minutes",
    "show_future_programs",
    "launch_on_startup",
    "auto_retry_failed_downloads",
}



@router.get("")
async def get_settings(
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Get app settings."""
    result = await session.execute(select(AppSettings))
    settings = result.scalar_one_or_none()

    if not settings:
        # Create default settings
        settings = AppSettings()
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if not settings.download_folder:
        settings.download_folder = app_settings.default_download_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    if is_docker_env() and not settings.download_folder.startswith("/app/"):
        settings.download_folder = app_settings.default_download_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    if not is_docker_env() and settings.download_folder.startswith("/app/"):
        settings.download_folder = app_settings.default_download_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    if not settings.completed_folder:
        settings.completed_folder = app_settings.default_completed_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    # Desktop builds now default to the OS Downloads directory.
    # Migrate legacy desktop defaults stored under CATCHUP_DATA_ROOT.
    if is_desktop_env():
        legacy_download = legacy_desktop_download_folder()
        legacy_completed = legacy_desktop_completed_folder()

        if _paths_match(settings.download_folder, legacy_download):
            settings.download_folder = app_settings.default_download_folder
            session.add(settings)
            await session.commit()
            await session.refresh(settings)

        if _paths_match(settings.completed_folder, legacy_completed):
            settings.completed_folder = app_settings.default_completed_folder
            session.add(settings)
            await session.commit()
            await session.refresh(settings)
    if is_docker_env() and not settings.completed_folder.startswith("/app/"):
        settings.completed_folder = app_settings.default_completed_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    if not is_docker_env() and settings.completed_folder.startswith("/app/"):
        settings.completed_folder = app_settings.default_completed_folder
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if settings.default_pre_padding_minutes is None or settings.default_pre_padding_minutes < 0:
        settings.default_pre_padding_minutes = 1
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if settings.min_free_space_gb is None or settings.min_free_space_gb < 1:
        settings.min_free_space_gb = 25
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if settings.default_post_padding_minutes is None or settings.default_post_padding_minutes < 0:
        settings.default_post_padding_minutes = 5
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    resolved_download_delay = resolve_scheduled_download_delay_minutes(
        settings.scheduled_download_delay_minutes
    )
    if settings.scheduled_download_delay_minutes != resolved_download_delay:
        settings.scheduled_download_delay_minutes = resolved_download_delay
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if settings.max_concurrent_post_processing is None:
        settings.max_concurrent_post_processing = 1
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    if settings.launch_on_startup is None:
        settings.launch_on_startup = True
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    config_dir = ensure_config_files()
    default_ini = config_dir / "comskip.ini"
    if settings.comskip_ini_path is None and default_ini.exists():
        settings.comskip_ini_path = str(default_ini)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    return settings.to_dict()


@router.put("")
async def update_settings(
    update_data: SettingsUpdate,
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session)
):
    """Update app settings."""
    result = await session.execute(select(AppSettings))
    settings = result.scalar_one_or_none()

    if not settings:
        settings = AppSettings()
        session.add(settings)

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        if value is None and field in NON_NULLABLE_FIELDS:
            continue
        if field == "default_account_id" and value is not None:
            value = int(value)
            account_result = await session.execute(
                select(XtreamAccount).where(XtreamAccount.id == value)
            )
            account = account_result.scalar_one_or_none()
            if account is None:
                raise HTTPException(status_code=400, detail="Default account not found")
        if field == "epg_offset_minutes" and value is not None:
            value = int(value)
        if field == "max_concurrent_post_processing" and value is not None:
            value = max(1, int(value))
        if field in {
            "default_pre_padding_minutes",
            "default_post_padding_minutes",
            "scheduled_download_delay_minutes",
        } and value is not None:
            value = int(value)
        if field == "min_free_space_gb" and value is not None:
            value = int(value)
        if field == "comskip_thread_count" and value is not None:
            value = max(1, min(16, int(value)))
        if field == "comskip_hw_decode_mode" and value is not None:
            value = normalize_comskip_hw_decode_mode(value)
        if field == "comskip_custom_ini_path" and value is not None:
            value = value.strip() or None
        setattr(settings, field, value)

    # Enforce ComSkip constraint on the final stored state: the cut needs
    # FFmpeg, so transcode_enabled must stay True.  This applies to Cut mode
    # ONLY (comskip_enabled AND comskip_cut).  Mark mode (comskip_cut=False)
    # does not cut, so it honours the format picker — including Keep .ts — and
    # never forces a re-encode (see docs/adr/0001-commercial-skip-mark-mode.md).
    # remux_only is NOT forced off: the pipeline supports stream-copy commercial
    # removal (segment extraction + concat with -c copy), so remux_only=True +
    # Cut is the valid "fast remux + skip commercials" profile from onboarding.
    if settings.comskip_enabled and settings.comskip_cut:
        settings.transcode_enabled = True

    if settings.comskip_use_custom_ini:
        custom_ini_path = (settings.comskip_custom_ini_path or "").strip()
        if not custom_ini_path:
            raise HTTPException(
                status_code=400,
                detail="A custom Comskip INI path is required when custom INI mode is enabled",
            )
        try:
            settings.comskip_custom_ini_path = validate_comskip_ini_path(
                custom_ini_path, custom=True
            )
        except ComskipIniError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate min<=max pairs on the final stored state so two separate PUT
    # requests cannot sneak an inverted range past per-request validation.
    def _pair_inverted(min_value, max_value) -> bool:
        return min_value is not None and max_value is not None and min_value > max_value

    if _pair_inverted(settings.comskip_min_commercialbreak, settings.comskip_max_commercialbreak):
        raise HTTPException(
            status_code=400,
            detail="Min commercial break cannot exceed max commercial break",
        )
    if _pair_inverted(settings.comskip_min_commercial_size, settings.comskip_max_commercial_size):
        raise HTTPException(
            status_code=400,
            detail="Min single commercial cannot exceed max single commercial",
        )

    if _paths_match(settings.download_folder, settings.completed_folder):
        logger.warning(
            "Download folder and completed folder are set to the same path (%s). "
            "Finished recordings will stay in place instead of being moved.",
            settings.download_folder,
        )

    await session.commit()
    await session.refresh(settings)

    # Update download manager if max concurrent changed
    if update_data.max_concurrent_downloads is not None:
        download_manager.set_max_concurrent(update_data.max_concurrent_downloads)
    if update_data.max_concurrent_post_processing is not None:
        download_manager.set_max_concurrent_post_processing(update_data.max_concurrent_post_processing)

    if "epg_offset_minutes" in update_dict:
        epg_service.clear_cache()

    return settings.to_dict()


@router.get("/folders/status")
async def get_folders_status(
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Report the resolved recording folders with existence and write-test results.

    Uses the same folder resolution as the download pipeline so the reported
    paths are exactly where recordings will be written.
    """
    result = await session.execute(select(AppSettings))
    settings_row = result.scalar_one_or_none()
    download_folder = download_manager._resolve_download_folder(settings_row)
    completed_folder = download_manager._resolve_completed_folder(settings_row)
    return {
        "download_folder": await asyncio.to_thread(_probe_folder_writable, download_folder),
        "completed_folder": await asyncio.to_thread(_probe_folder_writable, completed_folder),
    }


@router.get("/templates")
async def get_template_variables(_admin: None = Depends(require_admin)):
    """Get available template variables for filename customization."""
    return {
        "tv_show": {
            "variables": [
                {"name": "show", "description": "The show name"},
                {"name": "season", "description": "Season number (use :02d for zero-padding)"},
                {"name": "episode", "description": "Episode number (use :02d for zero-padding)"},
                {"name": "title", "description": "Episode title"},
                {"name": "date", "description": "Air date (YYYY-MM-DD)"},
            ],
            "example": "{show} - S{season:02d}E{episode:02d} - {title}",
            "rendered_example": "Breaking Bad - S01E05 - Gray Matter",
        },
        "movie": {
            "variables": [
                {"name": "title", "description": "Movie title"},
                {"name": "year", "description": "Release year"},
            ],
            "example": "{title} ({year})",
            "rendered_example": "Inception (2010)",
        },
        "sports": {
            "variables": [
                {"name": "title", "description": "Event title (e.g., 'NFL - Dolphins vs Chargers')"},
                {"name": "date", "description": "Event date (YYYY-MM-DD)"},
                {"name": "channel", "description": "Channel name"},
            ],
            "example": "{title} - {date}",
            "rendered_example": "FA Cup Final - 2024-01-14",
        },
        "default": {
            "variables": [
                {"name": "channel", "description": "Channel name"},
                {"name": "title", "description": "Program title"},
                {"name": "date", "description": "Air date (YYYY-MM-DD)"},
            ],
            "example": "{title} - {date}",
            "rendered_example": "Planet Earth - 2024-01-14",
        },
    }


@router.get("/tools")
async def get_tools_status(
    _admin: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Check availability of post-processing tools."""
    tool_status = post_processor.get_tool_runtime_status()
    result = await session.execute(select(AppSettings))
    settings = result.scalar_one_or_none()
    vaapi_status = post_processor.get_vaapi_diagnostics(
        settings.vaapi_render_device if settings else None
    )
    ffmpeg_status = tool_status["ffmpeg"]
    ffprobe_status = tool_status["ffprobe"]
    comskip_status = tool_status["comskip"]

    return {
        "ffmpeg": {
            "available": ffmpeg_status["available"],
            "path": ffmpeg_status["path"],
            "error": ffmpeg_status["error"],
            "description": "Required for transcoding to MP4/MKV formats",
            "install_hint": "Included in the Docker image; install ffmpeg if running locally.",
        },
        "ffprobe": {
            "available": ffprobe_status["available"],
            "path": ffprobe_status["path"],
            "error": ffprobe_status["error"],
            "description": "Used for duration/progress and segment timing calculations",
            "install_hint": "Usually installed with ffmpeg.",
        },
        "comskip": {
            "available": comskip_status["available"],
            "path": comskip_status["path"],
            "error": comskip_status["error"],
            "description": "Commercial detection and removal",
            "install_hint": "Included in the Docker image; build comskip if running locally.",
        },
        "transcode_formats": ["ts", "mp4", "mkv"],
        "hardware_accels": post_processor.get_available_hardware_accels(),
        "comskip_hw_decode_modes": post_processor.get_comskip_hw_decode_modes(),
        "vaapi": vaapi_status,
    }


@router.get("/public")
async def get_public_settings(
    auth: AuthContext = Depends(require_authenticated),
    session: AsyncSession = Depends(get_session),
):
    """Get safe settings values used by public browsing/download flows."""
    result = await session.execute(select(AppSettings))
    settings = result.scalar_one_or_none()

    if not settings:
        settings = AppSettings()
        session.add(settings)
        await session.commit()
        await session.refresh(settings)

    show_future = bool(settings.show_future_programs)
    if not auth.is_admin and auth.user and auth.user.show_future_programs is not None:
        show_future = bool(auth.user.show_future_programs)

    return {
        "show_future_programs": show_future,
        "default_pre_padding_minutes": max(0, settings.default_pre_padding_minutes) if settings.default_pre_padding_minutes is not None else 1,
        "default_post_padding_minutes": max(0, settings.default_post_padding_minutes) if settings.default_post_padding_minutes is not None else 5,
        "scheduled_download_delay_minutes": resolve_scheduled_download_delay_minutes(
            settings.scheduled_download_delay_minutes
        ),
        "default_account_id": settings.default_account_id,
    }
