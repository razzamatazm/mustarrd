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
    episode_id: Optional[str] = None,
) -> str:
    safe_show = _sanitize_component(show_name)
    safe_title = _sanitize_component(episode_title) if episode_title else ""
    # Preserve raw values so negative season/episode numbers (common with
    # non-conforming providers) produce distinct paths rather than colliding
    # with genuine season=0/episode=0 entries.
    season_num = int(season or 0)
    episode_num = int(episode or 0)

    season_folder = f"Season {season_num:02d}"
    base_name = f"S{season_num:02d}E{episode_num:02d} - {safe_show}"
    if safe_title:
        base_name = f"{base_name} - {safe_title}"
    elif season_num == 0 and episode_num == 0 and episode_id:
        # No usable metadata: append the provider episode ID so multiple
        # zero-metadata episodes of the same show get distinct output paths.
        base_name = f"{base_name} - {_sanitize_component(str(episode_id))}"

    filename = f"{base_name}.{_safe_extension(extension)}"

    return os.path.join(download_folder, safe_show, season_folder, filename)
