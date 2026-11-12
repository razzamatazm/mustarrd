"""The notification centre (#430): store, sources, audience, dismissal, cleanup.

Driven against a real in-memory SQLite database. The service functions take a
session; the bus subscriber and the sweeper take a session factory.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api import notifications as notifications_api  # noqa: E402
from auth import AuthContext  # noqa: E402
from database import Base  # noqa: E402
from models import Download, DownloadStatus, User, XtreamAccount  # noqa: E402
from models.notification import Notification, NotificationDismissal  # noqa: E402
from services import notifications as svc  # noqa: E402
from services.recording_events import (  # noqa: E402
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    RecordingEventBus,
)

NOW = datetime(2026, 11, 12, 12, 0, 0)


def _admin(user_id):
    return AuthContext(authenticated=True, role="admin", user_id=user_id, is_admin=True)


def _viewer(user_id):
    return AuthContext(authenticated=True, role="download_only", user_id=user_id, is_admin=False)


class NotificationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                User(id=1, username="admin", role="admin", status="active"),
                User(id=2, username="alice", role="download_only", status="active"),
                User(id=3, username="bob", role="download_only", status="active"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _account(self, **fields):
        defaults = dict(
            name="Trex", server_url="http://p.test", username="u", password="",
            is_active=True,
        )
        defaults.update(fields)
        async with self.Session() as session:
            account = XtreamAccount(**defaults)
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account

    async def _visible(self, context):
        async with self.Session() as session:
            return await svc.list_for(session, context)

    async def _all_rows(self):
        async with self.Session() as session:
            return (await session.execute(select(Notification))).scalars().all()


class AccountExpiryTests(NotificationTestCase):
    async def _sync(self, account, now=NOW):
        async with self.Session() as session:
            fresh = await session.get(XtreamAccount, account.id)
            await svc.sync_account_expiry(session, fresh, now=now)
            await session.commit()

    async def test_an_account_expiring_within_seven_days_raises_one_warning(self):
        account = await self._account(expiration_date=NOW + timedelta(days=3))
        await self._sync(account)
        await self._sync(account)  # a second pass must not duplicate it

        items = await self._visible(_admin(1))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], svc.KIND_ACCOUNT_EXPIRING)
        self.assertEqual(items[0]["severity"], "warning")
        self.assertIn("Trex", items[0]["message"])
        self.assertIn("3 days", items[0]["message"])

    async def test_an_account_far_from_expiry_raises_nothing(self):
        account = await self._account(expiration_date=NOW + timedelta(days=30))
        await self._sync(account)
        self.assertEqual(await self._visible(_admin(1)), [])

    async def test_an_account_with_no_expiry_date_raises_nothing(self):
        account = await self._account(expiration_date=None)
        await self._sync(account)
        self.assertEqual(await self._visible(_admin(1)), [])

    async def test_an_expired_account_raises_an_error(self):
        account = await self._account(expiration_date=NOW - timedelta(days=1))
        await self._sync(account)
        items = await self._visible(_admin(1))
        self.assertEqual([i["kind"] for i in items], [svc.KIND_ACCOUNT_EXPIRED])
        self.assertEqual(items[0]["severity"], "error")

    async def test_renewing_the_account_clears_the_notification(self):
        account = await self._account(expiration_date=NOW + timedelta(days=2))
        await self._sync(account)
        async with self.Session() as session:
            fresh = await session.get(XtreamAccount, account.id)
            fresh.expiration_date = NOW + timedelta(days=365)
            await session.commit()
        await self._sync(account)
        self.assertEqual(await self._all_rows(), [])

    async def test_removing_the_date_clears_the_notification(self):
        account = await self._account(expiration_date=NOW + timedelta(days=2))
        await self._sync(account)
        async with self.Session() as session:
            fresh = await session.get(XtreamAccount, account.id)
            fresh.expiration_date = None
            await session.commit()
        await self._sync(account)
        self.assertEqual(await self._all_rows(), [])

    async def test_an_inactive_account_does_not_notify(self):
        account = await self._account(expiration_date=NOW + timedelta(days=2), is_active=False)
        await self._sync(account)
        self.assertEqual(await self._all_rows(), [])

    async def test_going_from_expiring_to_expired_resurfaces_a_dismissed_warning(self):
        account = await self._account(expiration_date=NOW + timedelta(days=1))
        await self._sync(account)
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
        self.assertEqual(await self._visible(_admin(1)), [])

        await self._sync(account, now=NOW + timedelta(days=2))
        items = await self._visible(_admin(1))
        self.assertEqual([i["kind"] for i in items], [svc.KIND_ACCOUNT_EXPIRED])

    async def test_the_daily_countdown_does_not_resurface_a_dismissed_warning(self):
        account = await self._account(expiration_date=NOW + timedelta(days=6))
        await self._sync(account)
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
        await self._sync(account, now=NOW + timedelta(days=1))
        self.assertEqual(await self._visible(_admin(1)), [])

    async def test_download_only_users_never_see_account_problems(self):
        account = await self._account(expiration_date=NOW - timedelta(days=1))
        await self._sync(account)
        self.assertEqual(await self._visible(_viewer(2)), [])


class ConnectionFailureTests(NotificationTestCase):
    async def _sync(self, account_id, ok, error=None):
        async with self.Session() as session:
            account = await session.get(XtreamAccount, account_id)
            account.last_connection_ok = ok
            account.last_connection_error = error
            await svc.sync_account_connection(session, account)
            await session.commit()

    async def test_a_failed_check_raises_an_error_with_the_reason(self):
        account = await self._account()
        await self._sync(account.id, False, "Invalid credentials.")
        [item] = await self._visible(_admin(1))
        self.assertEqual(item["kind"], svc.KIND_ACCOUNT_CONNECTION)
        self.assertEqual(item["severity"], "error")
        self.assertIn("Invalid credentials.", item["message"])
        self.assertIn("Trex", item["title"])

    async def test_repeated_failures_keep_one_row_and_stay_dismissed(self):
        account = await self._account()
        await self._sync(account.id, False, "timeout")
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
        await self._sync(account.id, False, "timeout again")
        self.assertEqual(await self._visible(_admin(1)), [])
        self.assertEqual(len(await self._all_rows()), 1)

    async def test_a_successful_check_clears_it(self):
        account = await self._account()
        await self._sync(account.id, False, "timeout")
        await self._sync(account.id, True)
        self.assertEqual(await self._all_rows(), [])

    async def test_failing_again_after_recovering_shows_it_again(self):
        account = await self._account()
        await self._sync(account.id, False, "timeout")
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
        await self._sync(account.id, True)
        await self._sync(account.id, False, "timeout")
        self.assertEqual(len(await self._visible(_admin(1))), 1)

    async def test_the_ingest_manager_raises_it(self):
        """The real write site, not just the service, is wired up."""
        from unittest.mock import patch

        from services.epg_ingest_manager import EPGIngestManager

        account = await self._account()
        with patch("services.epg_ingest_manager.async_session_maker", self.Session):
            await EPGIngestManager()._update_connection_status(account.id, ok=False, error="boom")
        self.assertEqual(len(await self._visible(_admin(1))), 1)
        with patch("services.epg_ingest_manager.async_session_maker", self.Session):
            await EPGIngestManager()._update_connection_status(account.id, ok=True)
        self.assertEqual(await self._visible(_admin(1)), [])


class RecordingFailureTests(NotificationTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bus = RecordingEventBus()
        self.bus.subscribe(svc.NotificationSubscriber(session_maker=self.Session))

    def _recording(self, download_id=7, requested_by=2, **extra):
        payload = {
            "id": download_id,
            "program_title": "The News",
            "channel_name": "BBC One",
            "status": "failed",
            "error_message": "Provider refused the catchup request",
            "requested_by_user_id": requested_by,
            "is_vod": False,
        }
        payload.update(extra)
        return payload

    async def test_a_failed_recording_notifies_its_requester_and_admins(self):
        self.bus.publish(RECORDING_FAILED, self._recording())
        await self.bus.drain_once()

        for context in (_viewer(2), _admin(1)):
            with self.subTest(user=context.user_id):
                [item] = await self._visible(context)
                self.assertEqual(item["kind"], svc.KIND_RECORDING_FAILED)
                self.assertIn("The News", item["message"])
                self.assertIn("Provider refused", item["message"])
                self.assertEqual(item["link"], "/downloads")
        self.assertEqual(await self._visible(_viewer(3)), [])

    async def test_other_events_raise_nothing(self):
        self.bus.publish(RECORDING_STARTED, self._recording())
        await self.bus.drain_once()
        self.assertEqual(await self._all_rows(), [])

    async def test_a_retry_that_succeeds_clears_the_failure(self):
        self.bus.publish(RECORDING_FAILED, self._recording())
        self.bus.publish(RECORDING_COMPLETED, self._recording(status="completed"))
        await self.bus.drain_once()
        self.assertEqual(await self._all_rows(), [])

    async def test_failing_again_resurfaces_a_dismissed_failure(self):
        self.bus.publish(RECORDING_FAILED, self._recording())
        await self.bus.drain_once()
        [item] = await self._visible(_viewer(2))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _viewer(2))
        self.bus.publish(RECORDING_FAILED, self._recording())
        await self.bus.drain_once()
        self.assertEqual(len(await self._visible(_viewer(2))), 1)
        self.assertEqual(len(await self._all_rows()), 1)

    async def test_a_recording_with_no_requester_reaches_admins_only(self):
        self.bus.publish(RECORDING_FAILED, self._recording(requested_by=None))
        await self.bus.drain_once()
        self.assertEqual(len(await self._visible(_admin(1))), 1)
        self.assertEqual(await self._visible(_viewer(2)), [])

    async def test_a_real_pipeline_failure_reaches_the_subscriber(self):
        """Drive the download manager's own failure transition."""
        from unittest.mock import AsyncMock, patch

        from services.download_manager import DownloadManager

        async with self.Session() as session:
            download = Download(
                account_id=1, channel_id="1", channel_name="BBC One",
                program_title="The News",
                program_start=datetime(2026, 1, 1, 20), program_end=datetime(2026, 1, 1, 21),
                duration_minutes=60, source_url="http://p.test/x.ts", output_path="/tmp/x.ts",
                status=DownloadStatus.DOWNLOADING.value, requested_by_user_id=2,
            )
            session.add(download)
            await session.commit()
            download_id = download.id

        manager = DownloadManager()
        with patch("services.download_manager.recording_events", self.bus), \
                patch.object(manager, "_broadcast_progress", new_callable=AsyncMock):
            async with self.Session() as session:
                download = await session.get(Download, download_id)
                await manager._transition_failed(session, download, "it broke")
        await self.bus.drain_once()

        [item] = await self._visible(_viewer(2))
        self.assertIn("it broke", item["message"])


class DismissalTests(NotificationTestCase):
    async def _raise_admin_one(self, key="k1"):
        async with self.Session() as session:
            await svc.raise_notification(
                session, key=key, kind=svc.KIND_ACCOUNT_CONNECTION, severity="error",
                title="t", message="m",
            )
            await session.commit()

    async def test_dismissal_is_per_user(self):
        async with self.Session() as session:
            session.add(User(id=4, username="admin2", role="admin", status="active"))
            await session.commit()
        await self._raise_admin_one()
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
        self.assertEqual(await self._visible(_admin(1)), [])
        self.assertEqual(len(await self._visible(_admin(4))), 1)

    async def test_dismissing_twice_is_harmless(self):
        await self._raise_admin_one()
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
            await svc.dismiss(session, item["id"], _admin(1))
        async with self.Session() as session:
            rows = (await session.execute(select(NotificationDismissal))).scalars().all()
        self.assertEqual(len(rows), 1)

    async def test_a_user_cannot_dismiss_what_they_cannot_see(self):
        await self._raise_admin_one()
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            found = await svc.dismiss(session, item["id"], _viewer(2))
        self.assertFalse(found)

    async def test_dismiss_all_only_touches_what_the_user_sees(self):
        await self._raise_admin_one("a")
        await self._raise_admin_one("b")
        async with self.Session() as session:
            count = await svc.dismiss_all(session, _admin(1))
        self.assertEqual(count, 2)
        self.assertEqual(await self._visible(_admin(1)), [])


class CleanupTests(NotificationTestCase):
    async def test_old_recording_failures_are_pruned(self):
        async with self.Session() as session:
            for download_id in (1, 2):
                session.add(Download(
                    id=download_id, account_id=1, channel_id="1", channel_name="c",
                    program_title="p", program_start=NOW, program_end=NOW,
                    duration_minutes=60, source_url="", output_path="",
                ))
            await session.commit()
            await svc.raise_notification(
                session, key="recording_failed:1", kind=svc.KIND_RECORDING_FAILED,
                severity="error", title="t", message="m", download_id=1,
                now=NOW - timedelta(days=31),
            )
            await svc.raise_notification(
                session, key="recording_failed:2", kind=svc.KIND_RECORDING_FAILED,
                severity="error", title="t", message="m", download_id=2,
                now=NOW - timedelta(days=1),
            )
            await session.commit()
            removed = await svc.prune(session, now=NOW)
            await session.commit()
        self.assertEqual(removed, 1)
        self.assertEqual([r.key for r in await self._all_rows()], ["recording_failed:2"])

    async def test_a_notification_for_a_deleted_download_is_pruned(self):
        async with self.Session() as session:
            await svc.raise_notification(
                session, key="recording_failed:99", kind=svc.KIND_RECORDING_FAILED,
                severity="error", title="t", message="m", download_id=99, now=NOW,
            )
            await session.commit()
            removed = await svc.prune(session, now=NOW)
            await session.commit()
        self.assertEqual(removed, 1)
        self.assertEqual(await self._all_rows(), [])

    async def test_clearing_removes_dismissals_too(self):
        account = await self._account()
        async with self.Session() as session:
            fresh = await session.get(XtreamAccount, account.id)
            fresh.last_connection_ok = False
            await svc.sync_account_connection(session, fresh)
            await session.commit()
        [item] = await self._visible(_admin(1))
        async with self.Session() as session:
            await svc.dismiss(session, item["id"], _admin(1))
            await svc.clear_for_account(session, account.id)
            await session.commit()
            rows = (await session.execute(select(NotificationDismissal))).scalars().all()
        self.assertEqual(rows, [])
        self.assertEqual(await self._all_rows(), [])

    async def test_the_sweep_checks_every_account_and_prunes(self):
        await self._account(name="Soon", expiration_date=NOW + timedelta(days=2))
        await self._account(name="Fine", expiration_date=NOW + timedelta(days=90))
        await svc.sweep(session_maker=self.Session, now=NOW)
        items = await self._visible(_admin(1))
        self.assertEqual(len(items), 1)
        self.assertIn("Soon", items[0]["message"])


class NotificationsApiTests(NotificationTestCase):
    async def test_list_dismiss_and_dismiss_all(self):
        for key in ("a", "b"):
            async with self.Session() as session:
                await svc.raise_notification(
                    session, key=key, kind=svc.KIND_ACCOUNT_CONNECTION, severity="error",
                    title=key, message="m",
                )
                await session.commit()

        async with self.Session() as session:
            listing = await notifications_api.list_notifications(_admin(1), session)
        self.assertEqual(listing["count"], 2)

        async with self.Session() as session:
            await notifications_api.dismiss_notification(listing["items"][0]["id"], _admin(1), session)
            listing = await notifications_api.list_notifications(_admin(1), session)
        self.assertEqual(listing["count"], 1)

        async with self.Session() as session:
            await notifications_api.dismiss_all_notifications(_admin(1), session)
            listing = await notifications_api.list_notifications(_admin(1), session)
        self.assertEqual(listing["count"], 0)

    async def test_dismissing_an_unknown_notification_is_404(self):
        async with self.Session() as session:
            with self.assertRaises(HTTPException) as caught:
                await notifications_api.dismiss_notification(999, _admin(1), session)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
