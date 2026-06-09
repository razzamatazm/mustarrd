import asyncio
import os
import shutil
from typing import Optional, Tuple
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from models import AppSettings


# A hung network mount can make os.path.exists / shutil.disk_usage stall for
# minutes; cap the wait so the event loop and its caller are never held hostage.
DISK_USAGE_TIMEOUT_SECONDS = 10.0


async def get_free_space_gb(folder: str, timeout: float = DISK_USAGE_TIMEOUT_SECONDS) -> Optional[float]:
    """Return free space in GB on *folder*, or None when it cannot be determined.

    This is a strictly read-only probe: it never creates *folder*. If the path
    is missing (e.g. an unmounted NAS share), creating it or measuring a parent
    would report the free space of the wrong disk — typically the container's
    root filesystem — so the check reports unavailable instead and callers
    decide how to fail safe. The blocking stat runs off the event loop with a
    timeout so a hung network mount cannot stall the application.
    """
    try:
        exists = await asyncio.wait_for(asyncio.to_thread(os.path.exists, folder), timeout)
        if not exists:
            return None
        usage = await asyncio.wait_for(asyncio.to_thread(shutil.disk_usage, folder), timeout)
    except (asyncio.TimeoutError, OSError):
        return None
    return usage.free / (1024 ** 3)


async def get_free_space_shortfall(folder: str, min_free_gb: float) -> Optional[Tuple[float, float]]:
    """Return (free_gb, min_free_gb) when *folder* has less free space than required, else None."""
    if not min_free_gb or min_free_gb <= 0:
        return None
    free_gb = await get_free_space_gb(folder)
    if free_gb is None:
        return None
    if free_gb < min_free_gb:
        return free_gb, min_free_gb
    return None


async def check_disk_space(session: AsyncSession) -> None:
    """Raise HTTP 507 if free space on the download folder is below min_free_space_gb."""
    settings_result = await session.execute(select(AppSettings))
    app_settings_row = settings_result.scalar_one_or_none()
    download_folder = (
        app_settings_row.download_folder
        if app_settings_row and app_settings_row.download_folder
        else settings.default_download_folder
    )
    min_free_gb = (
        app_settings_row.min_free_space_gb
        if app_settings_row and app_settings_row.min_free_space_gb is not None
        else 25
    )
    shortfall = await get_free_space_shortfall(download_folder, min_free_gb)
    if shortfall:
        free_gb, required_gb = shortfall
        raise HTTPException(
            status_code=507,
            detail=(
                f"Not enough disk space to start this download. "
                f"{free_gb:.1f} GB free, {required_gb} GB required. "
                f"Free up space or lower the minimum free space setting."
            ),
        )
