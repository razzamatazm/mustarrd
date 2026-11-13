"""Daily update check: tell admins when a newer Mustarrd build is out.

Mustarrd has no numbered releases. Every merge to main publishes a new
``latest`` Docker image, built with its commit in ``CATCHUP_BUILD_SHA``. So "a
newer version" means "main has commits this build was not made from", and
GitHub's compare API answers that in one request:
``GET /repos/razzamatazm/mustarrd/compare/{build_sha}...main``.

- ``ahead_by > 0``: raise the ``update_available`` notification (admins only).
- ``ahead_by == 0``: clear it. This includes a build newer than main.
- Diverged, 404, any HTTP or network error: leave things as they are.
- No build SHA (local dev, desktop) or the admin turned the check off: no
  request at all.

This talks to GitHub, not to an IPTV provider, so it sends its own
User-Agent rather than ``provider_request_headers()``. It sends nothing about
the install or the user: no token, no cookies, no identifiers beyond the
public commit the image was built from.
"""

import asyncio
import logging
import re
from typing import Any, Dict, Optional

import aiohttp
from sqlalchemy import select

from config import settings as app_config
from database import async_session_maker
from models.settings import AppSettings
from services.notifications import (
    SEVERITY_WARNING,
    clear_notification,
    raise_notification,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_REPO = "razzamatazm/mustarrd"
USER_AGENT = "Mustarrd-update-check"
REQUEST_TIMEOUT_SECONDS = 15

UPDATE_NOTIFICATION_KEY = "update_available"
KIND_UPDATE_AVAILABLE = "update_available"

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
STARTUP_DELAY_SECONDS = 60

_SHA_PATTERN = re.compile(r"[0-9a-f]{7,40}")

OUTCOME_NO_BUILD = "no_build"
OUTCOME_DISABLED = "disabled"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_NOT_ON_MAIN = "not_on_main"
OUTCOME_UPDATE_AVAILABLE = "update_available"
OUTCOME_UP_TO_DATE = "up_to_date"
OUTCOME_ERROR = "error"


def running_build_sha(value: Optional[str] = None) -> str:
    """The commit this build was made from, or "" if unknown or malformed.

    Anything that is not a plain hex commit hash is treated as unknown, so the
    value can never reshape the request URL.
    """
    raw = app_config.build_sha if value is None else value
    cleaned = (raw or "").strip().lower()
    return cleaned if _SHA_PATTERN.fullmatch(cleaned) else ""


def compare_page_url(build_sha: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/compare/{build_sha}...main"


async def fetch_comparison(build_sha: str, api_base: str = GITHUB_API) -> Optional[Dict[str, Any]]:
    """Ask GitHub how main compares to ``build_sha``. None on any failure."""
    url = f"{api_base}/repos/{GITHUB_REPO}/compare/{build_sha}...main"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
            # per_page=1: only ahead_by is needed, not the commit list.
            async with http.get(url, params={"per_page": "1"}) as response:
                if response.status == 404:
                    logger.debug(
                        "Update check: GitHub does not know build %s on main", build_sha
                    )
                    return None
                if response.status != 200:
                    logger.info("Update check: GitHub answered HTTP %s", response.status)
                    return None
                body = await response.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed check just waits for tomorrow
        logger.info("Update check could not reach GitHub: %s", exc)
        return None
    if not isinstance(body, dict):
        logger.info("Update check: unexpected response from GitHub")
        return None
    return body


async def _is_enabled(session) -> bool:
    value = (
        await session.execute(select(AppSettings.update_check_enabled).limit(1))
    ).scalar_one_or_none()
    return value is not False


def _message(ahead_by: int) -> str:
    changes = "1 change" if ahead_by == 1 else f"{ahead_by} changes"
    return (
        f"A newer version of Mustarrd is available ({changes} since this build). "
        "To update, pull the latest Docker image and restart the container."
    )


async def check_for_update(
    session_maker=None,
    *,
    build_sha: Optional[str] = None,
    api_base: str = GITHUB_API,
) -> str:
    """One check. Returns what happened, as an ``OUTCOME_*`` string. Never raises."""
    maker = session_maker or async_session_maker
    sha = running_build_sha(build_sha)
    if not sha:
        logger.debug("Update check skipped: no build commit recorded for this install")
        return OUTCOME_NO_BUILD

    try:
        async with maker() as session:
            if not await _is_enabled(session):
                return OUTCOME_DISABLED

        comparison = await fetch_comparison(sha, api_base=api_base)
        if comparison is None:
            return OUTCOME_UNAVAILABLE

        status = comparison.get("status")
        if status == "diverged":
            logger.debug("Update check: build %s is not on main", sha)
            return OUTCOME_NOT_ON_MAIN
        try:
            ahead_by = int(comparison.get("ahead_by") or 0)
        except (TypeError, ValueError):
            logger.info("Update check: unexpected response from GitHub")
            return OUTCOME_UNAVAILABLE

        async with maker() as session:
            if ahead_by > 0:
                await raise_notification(
                    session,
                    key=UPDATE_NOTIFICATION_KEY,
                    kind=KIND_UPDATE_AVAILABLE,
                    severity=SEVERITY_WARNING,
                    title="Update available",
                    message=_message(ahead_by),
                    link=compare_page_url(sha),
                )
                outcome = OUTCOME_UPDATE_AVAILABLE
            else:
                await clear_notification(session, UPDATE_NOTIFICATION_KEY)
                outcome = OUTCOME_UP_TO_DATE
            await session.commit()
        return outcome
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never take the app down over an update check
        logger.exception("Update check failed")
        return OUTCOME_ERROR


class UpdateChecker:
    """Background loop started in the app lifespan, next to the other managers.

    Checks once shortly after startup, then daily. ``request_check()`` runs
    one sooner, e.g. when an admin turns the check back on.
    """

    def __init__(
        self,
        interval_seconds: int = CHECK_INTERVAL_SECONDS,
        startup_delay_seconds: int = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._interval = interval_seconds
        self._startup_delay = startup_delay_seconds
        self._wake: Optional[asyncio.Event] = None

    def request_check(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def process_queue(self) -> None:
        self._wake = asyncio.Event()
        if not running_build_sha():
            logger.debug("Update checker idle: no build commit recorded for this install")
        await self._sleep(self._startup_delay)
        while True:
            await check_for_update()
            await self._sleep(self._interval)


update_checker = UpdateChecker()
