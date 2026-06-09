import asyncio
import os
import shutil
from typing import Optional, Tuple
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from models import AppSettings


async def get_free_space_shortfall(folder: str, min_free_gb: float) -> Optional[Tuple[float, float]]:
    """Return (free_gb, min_free_gb) when *folder* has less free space than required, else None."""
    if not min_free_gb or min_free_gb <= 0:
        return None
    if not await asyncio.to_thread(os.path.exists, folder):
        return None
    usage = await asyncio.to_thread(shutil.disk_usage, folder)
    free_gb = usage.free / (1024 ** 3)
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
