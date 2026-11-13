from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path
import os
import shutil
import sys
import secrets
from typing import Optional


def _default_data_root() -> Path:
    configured_root = os.environ.get("CATCHUP_DATA_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "data"

def _using_docker_paths() -> bool:
    if os.environ.get("CATCHUP_DOCKER") == "1":
        return True

    # Common runtime markers across Docker, Podman, and OCI containers.
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    container_hint = os.environ.get("container", "").strip().lower()
    if container_hint in {"docker", "podman", "containerd", "oci"}:
        return True

    # Fallback for runtimes that only expose cgroup/container IDs.
    cgroup_path = Path("/proc/1/cgroup")
    if cgroup_path.exists():
        try:
            cgroup_text = cgroup_path.read_text()
            if any(token in cgroup_text for token in ("docker", "podman", "containerd", "libpod", "kubepods")):
                return True
        except OSError:
            pass

    return False


def _using_desktop_mode() -> bool:
    return os.environ.get("CATCHUP_DESKTOP_MODE") == "1"


def is_docker_env() -> bool:
    return _using_docker_paths()


def is_desktop_env() -> bool:
    return _using_desktop_mode()


def _desktop_downloads_dir() -> Path:
    downloads_dir = Path.home() / "Downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    return downloads_dir


def legacy_desktop_download_folder() -> str:
    return str(_default_data_root() / "downloads")


def legacy_desktop_completed_folder() -> str:
    return str(_default_data_root() / "completed")


def _session_secret_file() -> Path:
    """Returns the path where the auto-generated session secret is persisted."""
    if _using_docker_paths():
        return Path("/app/config/session_secret")
    return _default_data_root() / "session_secret"


def _load_or_create_session_secret() -> str:
    """Load a persisted session secret or generate and save one on first run.

    The secret is stored in the config/data directory so it survives restarts
    without any user configuration. Set CATCHUP_SESSION_SECRET in the environment
    to override with a specific value.
    """
    secret_file = _session_secret_file()
    if secret_file.exists():
        secret = secret_file.read_text().strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(48)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret)
    return secret


def _default_database_url() -> str:
    if _using_docker_paths():
        return "sqlite+aiosqlite:////app/config/catchup_dvr.db"
    return f"sqlite+aiosqlite:///{_default_data_root() / 'catchup_dvr.db'}"


def _default_download_folder() -> str:
    if _using_docker_paths():
        return "/app/downloads"
    if _using_desktop_mode():
        return str(_desktop_downloads_dir())
    return str(_default_data_root() / "downloads")


def _default_completed_folder() -> str:
    if _using_docker_paths():
        return "/app/completed"
    if _using_desktop_mode():
        return str(_desktop_downloads_dir())
    return str(_default_data_root() / "completed")



class Settings(BaseSettings):
    app_name: str = "Catchup DVR"
    debug: bool = False
    # Auto-generated on first run and persisted to the config directory so
    # sessions survive restarts with no user action required. Override with
    # CATCHUP_SESSION_SECRET env var to supply your own value.
    session_secret: str = Field(default_factory=_load_or_create_session_secret)
    # Session cookie secure policy:
    # - auto (default): set Secure only for HTTPS requests (including reverse-proxy forwarded HTTPS)
    # - always: always set Secure
    # - never: never set Secure
    session_cookie_secure_mode: str = "auto"
    # Legacy override (if set): true => always, false => never.
    session_https_only: Optional[bool] = None
    allow_remote_setup: bool = False
    # Comma-separated list of allowed CORS origins for the frontend.
    # Override with CATCHUP_CORS_ORIGINS when the frontend is served from
    # a non-localhost address (e.g. "http://192.168.1.50:4178").
    cors_origins: str = "http://localhost:4178,http://127.0.0.1:4178"

    # Database
    database_url: str = _default_database_url()

    # Downloads
    default_download_folder: str = _default_download_folder()
    default_completed_folder: str = _default_completed_folder()
    max_concurrent_downloads: int = 2

    # How requests to an IPTV provider identify themselves. Some providers
    # drop anything that does not look like a media player; override with
    # CATCHUP_PROVIDER_USER_AGENT if yours wants a specific one.
    provider_user_agent: str = "VLC/3.0.21 LibVLC/3.0.21"

    # The commit this build was made from. Baked into published Docker images
    # (see backend/Dockerfile); empty for local and desktop builds, which turns
    # the update check off.
    build_sha: str = ""

    # EPG Cache
    epg_cache_ttl: int = 3600  # 1 hour in seconds
    epg_refresh_interval_hours: int = 8

    class Config:
        env_file = ".env"
        env_prefix = "CATCHUP_"


settings = Settings()

def _get_config_dir() -> Path:
    db_url = settings.database_url
    prefix = "sqlite+aiosqlite:///"
    if db_url.startswith(prefix):
        db_path = Path(db_url[len(prefix):])
        return db_path.parent
    return Path("./config")


def _resolve_bundled_comskip_ini() -> Path | None:
    candidates: list[Path] = []

    env_ini_path = os.environ.get("CATCHUP_BUNDLED_COMSKIP_INI")
    if env_ini_path:
        candidates.append(Path(env_ini_path).expanduser())

    meipass_root = getattr(sys, "_MEIPASS", None)
    if meipass_root:
        candidates.append(Path(meipass_root) / "comskip.ini")

    repo_root = Path(__file__).resolve().parents[1]
    candidates.append(repo_root / "comskip.ini")

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def ensure_config_files() -> Path:
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    bundled_ini = _resolve_bundled_comskip_ini()
    target_ini = config_dir / "comskip.ini"

    if bundled_ini and bundled_ini.exists() and not target_ini.exists():
        shutil.copyfile(bundled_ini, target_ini)

    return config_dir


# Ensure config + data directories exist
Path(settings.default_download_folder).mkdir(parents=True, exist_ok=True)
Path(settings.default_completed_folder).mkdir(parents=True, exist_ok=True)
ensure_config_files()
