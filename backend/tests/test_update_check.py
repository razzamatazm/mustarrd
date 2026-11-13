"""Daily update check (#430): tell admins when main has moved past this build.

There are no GitHub releases, and every merge to main publishes a new
``latest`` image, so "a newer version" means "commits on main that the image
this is running from was not built from". The image carries its commit as
``CATCHUP_BUILD_SHA``; the check asks GitHub's compare API how far main is
ahead of it and mirrors the answer into one admin notification.

GitHub is played by a real local HTTP server, so the request that goes over
the wire (path, headers) is what gets asserted. The notification store is a
real in-memory SQLite database.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from aiohttp import web  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.settings import SettingsUpdate, get_settings, update_settings  # noqa: E402
from auth import AuthContext  # noqa: E402
from config import settings as app_config  # noqa: E402
from database import Base  # noqa: E402
from models import User  # noqa: E402
from models.notification import Notification  # noqa: E402
from models.settings import AppSettings  # noqa: E402
from services import notifications as notifications_svc  # noqa: E402
from services import update_check  # noqa: E402

BUILD_SHA = "0123456789abcdef0123456789abcdef01234567"


class _FakeGitHub:
    """Answers the compare endpoint with whatever the test sets."""

    def __init__(self):
        self.status = 200
        self.body = {"status": "identical", "ahead_by": 0, "behind_by": 0}
        self.requests = []
        self._runner = None
        self.base_url = ""

    async def _compare(self, request):
        self.requests.append(request)
        if self.status != 200:
            return web.json_response({"message": "Not Found"}, status=self.status)
        return web.json_response(self.body)

    async def start(self):
        app = web.Application()
        app.router.add_get("/repos/{owner}/{repo}/compare/{spec}", self._compare)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self._runner.cleanup()


class UpdateCheckTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(User(id=1, username="admin", role="admin", status="active"))
            session.add(User(id=2, username="alice", role="download_only", status="active"))
            await session.commit()

        self.github = _FakeGitHub()
        await self.github.start()
        self.addAsyncCleanup(self.github.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _check(self, build_sha=BUILD_SHA):
        return await update_check.check_for_update(
            self.Session, build_sha=build_sha, api_base=self.github.base_url
        )

    async def _rows(self):
        async with self.Session() as session:
            return (await session.execute(select(Notification))).scalars().all()

    async def _set_enabled(self, enabled):
        async with self.Session() as session:
            session.add(AppSettings(update_check_enabled=enabled))
            await session.commit()


class UpdateAvailableTests(UpdateCheckTestCase):
    async def test_main_ahead_raises_one_admin_notification(self):
        self.github.body = {"status": "ahead", "ahead_by": 3, "behind_by": 0}

        outcome = await self._check()

        self.assertEqual(outcome, "update_available")
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.key, update_check.UPDATE_NOTIFICATION_KEY)
        self.assertEqual(row.kind, update_check.KIND_UPDATE_AVAILABLE)
        self.assertEqual(row.severity, "warning")
        self.assertEqual(row.title, "Update available")
        self.assertEqual(
            row.message,
            "A newer version of Mustarrd is available (3 changes since this build). "
            "To update, pull the latest Docker image and restart the container.",
        )
        self.assertEqual(
            row.link,
            f"https://github.com/razzamatazm/mustarrd/compare/{BUILD_SHA}...main",
        )
        self.assertIsNone(row.user_id)

    async def test_one_change_is_singular(self):
        self.github.body = {"status": "ahead", "ahead_by": 1, "behind_by": 0}

        await self._check()

        self.assertIn("(1 change since this build)", (await self._rows())[0].message)

    async def test_admins_see_it_and_download_only_users_do_not(self):
        self.github.body = {"status": "ahead", "ahead_by": 2, "behind_by": 0}
        await self._check()

        admin = AuthContext(authenticated=True, role="admin", user_id=1, is_admin=True)
        viewer = AuthContext(
            authenticated=True, role="download_only", user_id=2, is_admin=False
        )
        async with self.Session() as session:
            self.assertEqual(len(await notifications_svc.list_for(session, admin)), 1)
            self.assertEqual(await notifications_svc.list_for(session, viewer), [])

    async def test_more_changes_later_update_the_same_row_without_resurfacing(self):
        self.github.body = {"status": "ahead", "ahead_by": 2, "behind_by": 0}
        await self._check()
        admin = AuthContext(authenticated=True, role="admin", user_id=1, is_admin=True)
        async with self.Session() as session:
            row_id = (await notifications_svc.list_for(session, admin))[0]["id"]
            await notifications_svc.dismiss(session, row_id, admin)

        self.github.body = {"status": "ahead", "ahead_by": 5, "behind_by": 0}
        await self._check()

        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("5 changes", rows[0].message)
        async with self.Session() as session:
            self.assertEqual(await notifications_svc.list_for(session, admin), [])

    async def test_request_identifies_the_app_and_nothing_about_the_user(self):
        self.github.body = {"status": "ahead", "ahead_by": 1, "behind_by": 0}

        await self._check()

        self.assertEqual(len(self.github.requests), 1)
        request = self.github.requests[0]
        self.assertEqual(
            request.path, f"/repos/razzamatazm/mustarrd/compare/{BUILD_SHA}...main"
        )
        self.assertEqual(request.headers.get("User-Agent"), "Mustarrd-update-check")
        self.assertEqual(request.headers.get("Accept"), "application/vnd.github+json")
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)


class UpToDateTests(UpdateCheckTestCase):
    async def test_identical_clears_an_existing_notification(self):
        self.github.body = {"status": "ahead", "ahead_by": 4, "behind_by": 0}
        await self._check()
        self.assertEqual(len(await self._rows()), 1)

        self.github.body = {"status": "identical", "ahead_by": 0, "behind_by": 0}
        outcome = await self._check()

        self.assertEqual(outcome, "up_to_date")
        self.assertEqual(await self._rows(), [])

    async def test_identical_with_nothing_raised_stays_empty(self):
        outcome = await self._check()

        self.assertEqual(outcome, "up_to_date")
        self.assertEqual(await self._rows(), [])


class NotOnMainTests(UpdateCheckTestCase):
    async def test_diverged_build_does_nothing(self):
        self.github.body = {"status": "diverged", "ahead_by": 7, "behind_by": 2}

        outcome = await self._check()

        self.assertEqual(outcome, "not_on_main")
        self.assertEqual(await self._rows(), [])

    async def test_diverged_build_leaves_an_existing_notification_alone(self):
        self.github.body = {"status": "ahead", "ahead_by": 1, "behind_by": 0}
        await self._check()

        self.github.body = {"status": "diverged", "ahead_by": 7, "behind_by": 2}
        await self._check()

        self.assertEqual(len(await self._rows()), 1)

    async def test_unknown_commit_404_does_nothing(self):
        self.github.status = 404

        outcome = await self._check()

        self.assertEqual(outcome, "unavailable")
        self.assertEqual(await self._rows(), [])


class FailureTests(UpdateCheckTestCase):
    async def test_rate_limited_or_server_error_does_nothing(self):
        for status in (403, 500):
            with self.subTest(status=status):
                self.github.status = status
                self.assertEqual(await self._check(), "unavailable")
                self.assertEqual(await self._rows(), [])

    async def test_network_error_does_not_raise(self):
        # Nothing listens on port 1, so the connection is refused.
        outcome = await update_check.check_for_update(
            self.Session, build_sha=BUILD_SHA, api_base="http://127.0.0.1:1"
        )

        self.assertEqual(outcome, "unavailable")
        self.assertEqual(await self._rows(), [])

    async def test_a_body_that_is_not_a_comparison_does_nothing(self):
        self.github.body = ["not", "an", "object"]

        outcome = await self._check()

        self.assertEqual(outcome, "unavailable")
        self.assertEqual(await self._rows(), [])


class NoBuildTests(UpdateCheckTestCase):
    async def test_no_build_sha_makes_no_request(self):
        for sha in ("", "   ", None):
            with self.subTest(sha=sha):
                with patch.object(app_config, "build_sha", sha or ""):
                    outcome = await self._check(build_sha=sha)
                self.assertEqual(outcome, "no_build")
        self.assertEqual(self.github.requests, [])

    async def test_build_sha_comes_from_config_by_default(self):
        with patch.object(app_config, "build_sha", BUILD_SHA):
            await update_check.check_for_update(
                self.Session, api_base=self.github.base_url
            )

        self.assertEqual(len(self.github.requests), 1)

    async def test_a_value_that_is_not_a_commit_is_never_put_in_the_url(self):
        outcome = await self._check(build_sha="../../evil?x=1")

        self.assertEqual(outcome, "no_build")
        self.assertEqual(self.github.requests, [])


class DisabledTests(UpdateCheckTestCase):
    async def test_disabled_makes_no_request(self):
        await self._set_enabled(False)
        self.github.body = {"status": "ahead", "ahead_by": 3, "behind_by": 0}

        outcome = await self._check()

        self.assertEqual(outcome, "disabled")
        self.assertEqual(self.github.requests, [])
        self.assertEqual(await self._rows(), [])

    async def test_enabled_by_default_with_no_settings_row(self):
        self.github.body = {"status": "ahead", "ahead_by": 3, "behind_by": 0}

        self.assertEqual(await self._check(), "update_available")


class UpdateCheckSettingTests(UpdateCheckTestCase):
    async def test_new_install_has_it_on(self):
        async with self.Session() as session:
            fetched = await get_settings(None, session)

        self.assertIs(fetched["update_check_enabled"], True)

    async def test_turning_it_off_round_trips_and_clears_the_notification(self):
        self.github.body = {"status": "ahead", "ahead_by": 3, "behind_by": 0}
        await self._check()
        self.assertEqual(len(await self._rows()), 1)

        async with self.Session() as session:
            await update_settings(
                SettingsUpdate(update_check_enabled=False), None, session
            )
        async with self.Session() as session:
            fetched = await get_settings(None, session)

        self.assertIs(fetched["update_check_enabled"], False)
        self.assertEqual(await self._rows(), [])

    async def test_null_does_not_overwrite_it(self):
        async with self.Session() as session:
            result = await update_settings(
                SettingsUpdate(update_check_enabled=None), None, session
            )

        self.assertIs(result["update_check_enabled"], True)

    async def test_settings_report_the_running_build(self):
        with patch.object(app_config, "build_sha", BUILD_SHA):
            async with self.Session() as session:
                fetched = await get_settings(None, session)

        self.assertEqual(fetched["build_sha"], BUILD_SHA)

    async def test_turning_it_on_asks_for_a_check_now(self):
        with patch.object(update_check.update_checker, "request_check") as request_check:
            async with self.Session() as session:
                await update_settings(
                    SettingsUpdate(update_check_enabled=True), None, session
                )

        request_check.assert_called_once()


class UpdateCheckerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_the_startup_delay_unless_asked_and_cancels_cleanly(self):
        import asyncio

        ran = asyncio.Event()

        async def fake_check(*_args, **_kwargs):
            ran.set()
            return "up_to_date"

        checker = update_check.UpdateChecker(
            interval_seconds=3600, startup_delay_seconds=3600
        )
        with patch.object(update_check, "check_for_update", fake_check):
            task = asyncio.create_task(checker.process_queue())
            await asyncio.sleep(0.05)
            self.assertFalse(ran.is_set())

            checker.request_check()
            await asyncio.wait_for(ran.wait(), timeout=2)

            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)


if __name__ == "__main__":
    unittest.main()
