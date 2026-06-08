import os
import re
from typing import Optional

from services.file_namer import file_namer


def _sanitize_component(value: str) -> str:
    return file_namer.sanitize_filename(value or "Unknown")


def _safe_extension(ext: Optional[str]) -> str:
    if not ext:
        return "mp4"
    cleaned = ext.strip().lstrip(".")
    if not cleaned:
        return "mp4"
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", cleaned):
        return "mp4"
    return cleaned.lower()


def movie_output_path(download_folder: str, title: str, year: Optional[int], extension: Optional[str]) -> str:
    safe_title = _sanitize_component(title)
    if year:
        title_base = re.sub(r'\s*\(\d{4}\)\s*$', '', safe_title)
        filename = f"{title_base} ({year})"
    else:
        filename = safe_title
    filename = f"{filename}.{_safe_extension(extension)}"
    return os.path.join(download_folder, filename)


def series_episode_output_path(
    download_folder: str,
    show_name: str,
    season: int,
    episode: int,
    episode_title: Optional[str],
    extension: Optional[str],
) -> str:
    safe_show = _sanitize_component(show_name)
    safe_title = _sanitize_component(episode_title) if episode_title else ""
    season_num = max(int(season or 0), 0)
    episode_num = max(int(episode or 0), 0)

    season_folder = f"Season {season_num:02d}"
    base_name = f"S{season_num:02d}E{episode_num:02d} - {safe_show}"
    if safe_title:
        base_name = f"{base_name} - {safe_title}"

    filename = f"{base_name}.{_safe_extension(extension)}"

    return os.path.join(download_folder, safe_show, season_folder, filename)
