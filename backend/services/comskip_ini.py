"""Build the comskip.ini used for commercial detection.

The Settings page exposes a small set of Comskip tunables stored on
app_settings (comskip_detect_method, comskip_min_commercialbreak, ...).
At detection time the INI passed to Comskip is produced by taking a base
INI — the user's config-dir comskip.ini if present, else the bundled one —
and overriding those tunable keys with the stored values.  Starting from
the base file keeps non-exposed keys working, most importantly
output_edl=1 which the post-processing pipeline depends on.

A user-supplied custom INI (comskip_custom_ini_path) bypasses generation
entirely and is passed to Comskip as-is.
"""
import logging
import os
import re
import tempfile
from typing import Optional

from config import ensure_config_files, _resolve_bundled_comskip_ini

logger = logging.getLogger(__name__)

GENERATED_INI_NAME = "comskip.generated.ini"

# comskip.ini keys editable in the Settings UI; each maps 1:1 to the
# AppSettings column with a comskip_ prefix.
TUNABLE_KEYS = (
    "detect_method",
    "max_commercialbreak",
    "min_commercialbreak",
    "max_commercial_size",
    "min_commercial_size",
    "always_keep_first_seconds",
    "always_keep_last_seconds",
    "remove_before",
    "remove_after",
    "thread_count",
)

_KEY_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def tunable_overrides(settings) -> dict[str, int]:
    """Read the tunable values off an AppSettings row, skipping unset ones."""
    overrides: dict[str, int] = {}
    for key in TUNABLE_KEYS:
        value = getattr(settings, f"comskip_{key}", None)
        if value is not None:
            overrides[key] = int(value)
    return overrides


def render_comskip_ini(base_text: str, overrides: dict[str, int]) -> str:
    """Return base_text with `key=value` lines replaced by the overrides.

    Keys not present in the base are appended at the end, so the result is
    complete even when the base INI is minimal.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in base_text.splitlines():
        match = _KEY_LINE.match(line)
        key = match.group(1) if match else None
        if key in overrides:
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in overrides if key not in seen]
    if missing:
        out.append("; Values below managed by the Mustarrd Comskip settings page")
        for key in missing:
            out.append(f"{key}={overrides[key]}")
    return "\n".join(out) + "\n"


def _base_ini_text() -> str:
    config_dir = ensure_config_files()
    candidates = [config_dir / "comskip.ini"]
    bundled = _resolve_bundled_comskip_ini()
    if bundled is not None:
        candidates.append(bundled)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s as comskip.ini base: %s", candidate, exc)
    # Minimal base: EDL output is required by the post-processing pipeline.
    return "output_edl=1\n"


def generate_comskip_ini(settings) -> Optional[str]:
    """Write the generated INI into the config dir and return its path.

    Returns None when generation fails (unwritable config dir, etc.) so the
    caller can fall back instead of breaking commercial detection.
    """
    try:
        config_dir = ensure_config_files()
        content = render_comskip_ini(_base_ini_text(), tunable_overrides(settings))
        target = config_dir / GENERATED_INI_NAME
        # Atomic replace: concurrent post-processing tasks may regenerate at
        # the same time, and Comskip must never read a half-written file.
        fd, tmp_path = tempfile.mkstemp(dir=str(config_dir), prefix=".comskip-ini-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return str(target)
    except Exception as exc:
        logger.warning("Failed to generate comskip.ini from settings: %s", exc)
        return None


def resolve_comskip_ini(settings) -> Optional[str]:
    """Pick the INI path to pass to Comskip for this run.

    Precedence: user custom INI > INI generated from stored settings >
    legacy comskip_ini_path (pre-editor behaviour, used only when
    generation fails).
    """
    custom = (getattr(settings, "comskip_custom_ini_path", None) or "").strip()
    if custom:
        return custom
    generated = generate_comskip_ini(settings)
    if generated:
        return generated
    return getattr(settings, "comskip_ini_path", None)
