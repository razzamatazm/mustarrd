"""The notification centre (#430): problems the user should hear about.

Three sources feed one table:

- **Account expiry.** Not event-shaped, so ``sweep()`` checks every account on
  a timer, and the accounts API checks one account whenever it is saved or
  refreshed. Expiring within ``EXPIRY_WARNING_DAYS`` is a warning; past the
  date is an error.
- **Account connection failure.** The EPG ingest manager writes
  ``last_connection_ok`` after each refresh and calls
  ``sync_account_connection`` in the same session.
- **Recording failure.** ``NotificationSubscriber`` listens on the recording
  event seam. Any transition to ``failed`` notifies, immediately; a later
  ``recording.completed`` for the same download (a retry that worked) clears it.

Rules every source shares:

- **One row per problem**, found by ``key``. Raising it again updates the row.
- **A problem that goes away deletes its row**, along with everyone's
  dismissals of it. That is the cleanup for state-shaped notifications; the
  only thing ``prune()`` has to age out is old recording failures.
- **Dismissal is per user.** It sticks while the problem persists. It is reset
  ("resurfaced") when the problem comes back after going away, when it gets
  worse (expiring -> expired), or when a recording fails again.
- **Audience.** Admins see everything. A download-only user sees rows whose
  ``user_id`` is theirs: failed recordings they requested.

Notification timestamps are UTC (``utcnow``) like the rest of the schema.
Expiry dates are compared in local time, because the accounts API stores them
with ``datetime.fromtimestamp``.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker
from models.account import XtreamAccount
from models.download import Download
from models.notification import Notification, NotificationDismissal
from services.recording_events import (
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RecordingEvent,
)

logger = logging.getLogger(__name__)

KIND_ACCOUNT_EXPIRING = "account_expiring"
KIND_ACCOUNT_EXPIRED = "account_expired"
KIND_ACCOUNT_CONNECTION = "account_connection_failed"
KIND_RECORDING_FAILED = "recording_failed"

SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"

EXPIRY_WARNING_DAYS = 7
RECORDING_NOTIFICATION_RETENTION_DAYS = 30
SWEEP_INTERVAL_SECONDS = 60 * 60

ACCOUNTS_LINK = "/settings?section=accounts"
DOWNLOADS_LINK = "/downloads"


def expiry_key(account_id: int) -> str:
    return f"account_expiry:{account_id}"


def connection_key(account_id: int) -> str:
    return f"account_connection:{account_id}"


def recording_key(download_id: int) -> str:
    return f"recording_failed:{download_id}"


# --- store ---------------------------------------------------------------

async def _get_by_key(session: AsyncSession, key: str) -> Optional[Notification]:
    result = await session.execute(select(Notification).where(Notification.key == key))
    return result.scalar_one_or_none()


async def raise_notification(
    session: AsyncSession,
    *,
    key: str,
    kind: str,
    severity: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    user_id: Optional[int] = None,
    account_id: Optional[int] = None,
    download_id: Optional[int] = None,
    resurface: bool = False,
    now: Optional[datetime] = None,
) -> Notification:
    """Create the notification for ``key``, or update the one already there.

    ``resurface`` clears every dismissal and moves it to the top, for a
    problem that is new again rather than merely still going on. A change of
    ``kind`` (expiring -> expired) always resurfaces. Does not commit.
    """
    stamp = now or datetime.utcnow()
    notification = await _get_by_key(session, key)
    if notification is None:
        notification = Notification(key=key, created_at=stamp)
        session.add(notification)
    elif resurface or notification.kind != kind:
        await session.execute(
            delete(NotificationDismissal).where(
                NotificationDismissal.notification_id == notification.id
            )
        )
        notification.created_at = stamp

    notification.kind = kind
    notification.severity = severity
    notification.title = title
    notification.message = message
    notification.link = link
    notification.user_id = user_id
    notification.account_id = account_id
    notification.download_id = download_id
    notification.updated_at = stamp
    await session.flush()
    return notification


async def _delete_where(session: AsyncSession, *criteria) -> int:
    ids = (await session.execute(select(Notification.id).where(*criteria))).scalars().all()
    if not ids:
        return 0
    await session.execute(
        delete(NotificationDismissal).where(NotificationDismissal.notification_id.in_(ids))
    )
    await session.execute(delete(Notification).where(Notification.id.in_(ids)))
    return len(ids)


async def clear_notification(session: AsyncSession, key: str) -> bool:
    """The problem went away. Does not commit."""
    return bool(await _delete_where(session, Notification.key == key))


async def clear_for_account(session: AsyncSession, account_id: int) -> int:
    """Remove an account's notifications, e.g. when the account is deleted."""
    return await _delete_where(
        session,
        Notification.key.in_([expiry_key(account_id), connection_key(account_id)]),
    )


# --- sources -------------------------------------------------------------

def _format_date(value: datetime) -> str:
    return f"{value:%b} {value.day}, {value.year}"


async def sync_account_expiry(
    session: AsyncSession,
    account: XtreamAccount,
    now: Optional[datetime] = None,
) -> None:
    """Raise, update or clear the expiry notification for one account."""
    local_now = now or datetime.now()
    key = expiry_key(account.id)
    expires = account.expiration_date

    if not account.is_active or expires is None:
        await clear_notification(session, key)
        return

    if expires <= local_now:
        await raise_notification(
            session,
            key=key,
            kind=KIND_ACCOUNT_EXPIRED,
            severity=SEVERITY_ERROR,
            title="Account expired",
            message=(
                f"{account.name} expired on {_format_date(expires)}. "
                "Recordings from it will fail until it is renewed."
            ),
            link=ACCOUNTS_LINK,
            account_id=account.id,
        )
        return

    if expires - local_now > timedelta(days=EXPIRY_WARNING_DAYS):
        await clear_notification(session, key)
        return

    days = (expires.date() - local_now.date()).days
    if days <= 0:
        when = "today"
    elif days == 1:
        when = "tomorrow"
    else:
        when = f"in {days} days"
    await raise_notification(
        session,
        key=key,
        kind=KIND_ACCOUNT_EXPIRING,
        severity=SEVERITY_WARNING,
        title="Account expiring soon",
        message=f"{account.name} expires {when}, on {_format_date(expires)}.",
        link=ACCOUNTS_LINK,
        account_id=account.id,
    )


async def sync_account_connection(session: AsyncSession, account: XtreamAccount) -> None:
    """Mirror ``last_connection_ok`` into a notification. Does not commit."""
    key = connection_key(account.id)
    if not account.is_active or account.last_connection_ok is not False:
        await clear_notification(session, key)
        return

    reason = (account.last_connection_error or "").strip()
    message = reason or "The last guide refresh could not connect to the provider."
    await raise_notification(
        session,
        key=key,
        kind=KIND_ACCOUNT_CONNECTION,
        severity=SEVERITY_ERROR,
        title=f"Can't connect to {account.name}",
        message=message,
        link=ACCOUNTS_LINK,
        account_id=account.id,
    )


def _recording_label(recording: Dict[str, Any]) -> str:
    title = (recording.get("program_title") or "").strip() or "A recording"
    channel = (recording.get("channel_name") or "").strip()
    if channel and not recording.get("is_vod"):
        return f"{title} on {channel}"
    return title


async def record_recording_failed(session: AsyncSession, recording: Dict[str, Any]) -> None:
    download_id = recording.get("id")
    if download_id is None:
        return
    label = _recording_label(recording)
    reason = (recording.get("error_message") or "").strip()
    message = f"{label}: {reason}" if reason else f"{label} did not finish."
    await raise_notification(
        session,
        key=recording_key(int(download_id)),
        kind=KIND_RECORDING_FAILED,
        severity=SEVERITY_ERROR,
        title="Recording failed",
        message=message,
        link=DOWNLOADS_LINK,
        user_id=recording.get("requested_by_user_id"),
        download_id=int(download_id),
        resurface=True,
    )


class NotificationSubscriber:
    """Recording event seam consumer. Persists on receipt; never raises."""

    def __init__(self, session_maker=None) -> None:
        self._session_maker = session_maker

    async def __call__(self, event: RecordingEvent) -> None:
        if event.name not in (RECORDING_FAILED, RECORDING_COMPLETED):
            return
        recording = event.recording or {}
        download_id = recording.get("id")
        if download_id is None:
            return
        maker = self._session_maker or async_session_maker
        try:
            async with maker() as session:
                if event.name == RECORDING_FAILED:
                    await record_recording_failed(session, recording)
                else:
                    await clear_notification(session, recording_key(int(download_id)))
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a notification is not the recording's problem
            logger.exception(
                "Could not record notification for %s on download %s", event.name, download_id
            )


def register_notification_subscriber(bus) -> NotificationSubscriber:
    """Put the notification consumer on the recording event bus. Called at startup."""
    subscriber = NotificationSubscriber()
    bus.subscribe(subscriber)
    return subscriber


# --- reading and dismissing ---------------------------------------------

def _visible_to(context):
    """SQL criteria for the notifications this user may see."""
    criteria = []
    if not context.is_admin:
        criteria.append(Notification.user_id == context.user_id)
    return criteria


async def list_for(session: AsyncSession, context) -> List[Dict[str, Any]]:
    """Notifications this user can see and has not dismissed, newest first."""
    if not context.user_id:
        return []
    dismissed = exists().where(
        NotificationDismissal.notification_id == Notification.id,
        NotificationDismissal.user_id == context.user_id,
    )
    result = await session.execute(
        select(Notification)
        .where(*_visible_to(context), ~dismissed)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    return [row.to_dict() for row in result.scalars().all()]


async def _dismiss_ids(session: AsyncSession, ids, user_id: int) -> None:
    already = set(
        (
            await session.execute(
                select(NotificationDismissal.notification_id).where(
                    NotificationDismissal.user_id == user_id,
                    NotificationDismissal.notification_id.in_(ids),
                )
            )
        ).scalars().all()
    )
    for notification_id in ids:
        if notification_id not in already:
            session.add(NotificationDismissal(notification_id=notification_id, user_id=user_id))


async def dismiss(session: AsyncSession, notification_id: int, context) -> bool:
    """Hide one notification for this user. False if they cannot see it. Commits."""
    if not context.user_id:
        return False
    found = (
        await session.execute(
            select(Notification.id).where(
                Notification.id == notification_id, *_visible_to(context)
            )
        )
    ).scalar_one_or_none()
    if found is None:
        return False
    await _dismiss_ids(session, [found], context.user_id)
    await session.commit()
    return True


async def dismiss_all(session: AsyncSession, context) -> int:
    """Hide everything this user currently sees. Returns how many. Commits."""
    items = await list_for(session, context)
    ids = [item["id"] for item in items]
    if ids:
        await _dismiss_ids(session, ids, context.user_id)
        await session.commit()
    return len(ids)


# --- housekeeping --------------------------------------------------------

async def prune(session: AsyncSession, now: Optional[datetime] = None) -> int:
    """Age out old recording failures and rows whose subject is gone. Does not commit."""
    stamp = now or datetime.utcnow()
    cutoff = stamp - timedelta(days=RECORDING_NOTIFICATION_RETENTION_DAYS)
    removed = await _delete_where(
        session,
        Notification.kind == KIND_RECORDING_FAILED,
        Notification.updated_at < cutoff,
    )
    removed += await _delete_where(
        session,
        Notification.download_id.is_not(None),
        ~exists().where(Download.id == Notification.download_id),
    )
    removed += await _delete_where(
        session,
        Notification.account_id.is_not(None),
        ~exists().where(XtreamAccount.id == Notification.account_id),
    )
    return removed


async def sweep(session_maker=None, now: Optional[datetime] = None) -> None:
    """One pass: expiry and connection state for every account, then prune.

    ``now`` is used for both clocks in tests; in production expiry compares in
    local time and pruning in UTC.
    """
    maker = session_maker or async_session_maker
    async with maker() as session:
        accounts = (await session.execute(select(XtreamAccount))).scalars().all()
        for account in accounts:
            await sync_account_expiry(session, account, now=now)
            await sync_account_connection(session, account)
        await prune(session, now=now)
        await session.commit()


class NotificationSweeper:
    """Background loop started in the app lifespan, next to the other managers."""

    def __init__(self, interval_seconds: int = SWEEP_INTERVAL_SECONDS) -> None:
        self._interval = interval_seconds

    async def process_queue(self) -> None:
        logger.info("Notification sweeper started")
        while True:
            try:
                await sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - try again next pass
                logger.exception("Notification sweep failed")
            await asyncio.sleep(self._interval)


notification_sweeper = NotificationSweeper()
