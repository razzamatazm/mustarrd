import asyncio
import os
import shutil
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from models import AppSettings


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
    if min_free_gb and min_free_gb > 0 and await asyncio.to_thread(os.path.exists, download_folder):
        usage = await asyncio.to_thread(shutil.disk_usage, download_folder)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_free_gb:
            raise HTTPException(
                status_code=507,
                detail=(
                    f"Not enough disk space to start this download. "
                    f"{free_gb:.1f} GB free, {min_free_gb} GB required. "
                    f"Free up space or lower the minimum free space setting."
                ),
            )
