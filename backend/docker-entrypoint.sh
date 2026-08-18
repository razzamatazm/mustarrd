#!/usr/bin/env bash
set -euo pipefail

# Default to common Linux user IDs, but auto-switch to Unraid nobody:users
# when running on Unraid and PUID/PGID were not provided.
if [[ -z "${PUID:-}" || -z "${PGID:-}" ]]; then
  if [[ -f /etc/unraid-version ]]; then
    PUID="${PUID:-99}"
    PGID="${PGID:-100}"
  else
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
  fi
else
  PUID="${PUID}"
  PGID="${PGID}"
fi
APP_USER="appuser"
APP_GROUP="appgroup"
if [[ -n "${LIBVA_DRIVER_NAME:-}" ]]; then
  export LIBVA_DRIVER_NAME
fi

ensure_owned_path() {
  local path="$1"
  mkdir -p "${path}" 2>/dev/null || true

  if ! chown "${PUID}:${PGID}" "${path}" 2>/dev/null; then
    echo "WARN: could not chown ${path} to ${PUID}:${PGID}" >&2
  fi

  if ! chown -R "${PUID}:${PGID}" "${path}" 2>/dev/null; then
    echo "WARN: could not recursively chown ${path} to ${PUID}:${PGID}" >&2
  fi
}

is_mountpoint_path() {
  local path="$1"
  awk -v target="${path}" '$2 == target { found=1 } END { exit found ? 0 : 1 }' /proc/mounts
}

ensure_group() {
  local name="$1"
  local gid="$2"

  if getent group "${gid}" >/dev/null 2>&1; then
    return 0
  fi
  if getent group "${name}" >/dev/null 2>&1; then
    groupmod -g "${gid}" "${name}"
    return 0
  fi
  groupadd -g "${gid}" "${name}"
}

if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd -g "${PGID}" "${APP_GROUP}"
else
  APP_GROUP="$(getent group "${PGID}" | cut -d: -f1)"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd -m -u "${PUID}" -g "${PGID}" -s /bin/bash "${APP_USER}"
else
  usermod -u "${PUID}" -g "${PGID}" "${APP_USER}"
fi

# Grant access to every DRM node present, not just the first one. The render
# device is chosen at runtime in Settings, which happens long after this script
# runs, so the container cannot know in advance which node ffmpeg will open.
grant_drm_access() {
  local pattern="$1" group_base="$2" node gid seen=""
  for node in ${pattern}; do
    [[ -e "${node}" ]] || continue
    gid="$(stat -c '%g' "${node}")" || continue
    case " ${seen} " in
      *" ${gid} "*) continue ;;
    esac
    seen="${seen} ${gid}"
    local group_name="${group_base}"
    if [[ -n "${seen% ${gid}}" ]]; then
      group_name="${group_base}${gid}"
    fi
    ensure_group "${group_name}" "${gid}"
    usermod -aG "${group_name}" "${APP_USER}"
  done
}

grant_drm_access "/dev/dri/renderD*" "render"
grant_drm_access "/dev/dri/card*" "video"

ensure_owned_path /app/config
ensure_owned_path /app/downloads
ensure_owned_path /app/completed

# Unraid/single-mount fallback: if only /app/config is bind-mounted, keep
# downloads + completed under that mounted config root.
if is_mountpoint_path /app/config && ! is_mountpoint_path /app/downloads && ! is_mountpoint_path /app/completed; then
  export CATCHUP_DEFAULT_DOWNLOAD_FOLDER="/app/config/downloads"
  export CATCHUP_DEFAULT_COMPLETED_FOLDER="/app/config/completed"
  ensure_owned_path "${CATCHUP_DEFAULT_DOWNLOAD_FOLDER}"
  ensure_owned_path "${CATCHUP_DEFAULT_COMPLETED_FOLDER}"
fi

if [[ -f /app/comskip.ini && ! -f /app/config/comskip.ini ]]; then
  cp /app/comskip.ini /app/config/comskip.ini
  if ! chown "${PUID}:${PGID}" /app/config/comskip.ini 2>/dev/null; then
    echo "WARN: could not chown /app/config/comskip.ini to ${PUID}:${PGID}" >&2
  fi
fi

exec gosu "${APP_USER}" "$@"
