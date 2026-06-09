"""
Tests for per-user starred channels in the Browse channel list.

Feature: users can star channels via POST
/accounts/{account_id}/channels/{channel_id}/star. Stars are stored per user
in the starred_channels table, the channel list response carries a `starred`
flag for the requesting user, and starred channels sort to the top of the
list while the provider's order is preserved for everything else.
"""
import inspect
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.channels import get_channels, toggle_channel_star
from auth import AuthContext, require_admin_or_download_user
from database import Base
from models import StarredChannel, User, XtreamAccount


def _make_account(name="Provider"):
    return XtreamAccount(
        name=name,
        server_url="http://provider.example:8080",
        username="user",
        password="",
        password_encrypted="enc",
    )


def _make_user(display_name, role="download_only"):
    return User(
        role=role,
        status="active",
        display_name=display_name,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_auth(user_id, is_admin=False):
    return AuthContext(
        authenticated=True,
        role="admin" if is_admin else "download_only",
        user_id=user_id,
        is_admin=is_admin,
    )


def _fake_channels():
    return [
        {"stream_id": 101, "name": "Alpha", "tv_archive": 1, "tv_archive_duration": 7},
        {"stream_id": 102, "name": "Bravo", "tv_archive": 1, "tv_archive_duration": 7},
        {"stream_id": 103, "name": "Charlie", "tv_archive": 1, "tv_archive_duration": 7},
    ]


def _make_xtream_client(channels):
    client = MagicMock()
    client.get_live_streams = AsyncMock(return_value=channels)
    client.close = AsyncMock()
    return client


class StarredChannelDBTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_factory() as session:
            account = _make_account()
            user_a = _make_user("User A")
            user_b = _make_user("User B")
            session.add_all([account, user_a, user_b])
            await session.commit()
            await session.refresh(account)
            await session.refresh(user_a)
            await session.refresh(user_b)
            self.account_id = account.id
            self.user_a_id = user_a.id
            self.user_b_id = user_b.id

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _count_stars(self, user_id):
        async with self.session_factory() as session:
            result = await session.execute(
                select(StarredChannel).where(StarredChannel.user_id == user_id)
            )
            return len(result.scalars().all())

    async def _get_channels(self, user_id):
        client = _make_xtream_client(_fake_channels())
        with (
            patch("api.channels.XtreamClient", return_value=client),
            patch(
                "api.channels.resolve_account_password_with_migration",
                AsyncMock(return_value="pw"),
            ),
        ):
            async with self.session_factory() as session:
                return await get_channels(
                    account_id=self.account_id,
                    category_id=None,
                    catchup_only=False,
                    auth=_make_auth(user_id),
                    session=session,
                )

    # ------------------------------------------------------------------
    # toggle endpoint
    # ------------------------------------------------------------------

    async def test_toggle_star_creates_then_removes(self):
        """First toggle stars the channel, second toggle removes the star."""
        async with self.session_factory() as session:
            result = await toggle_channel_star(
                account_id=self.account_id,
                channel_id="103",
                auth=_make_auth(self.user_a_id),
                session=session,
            )
        self.assertEqual(result, {"starred": True})
        self.assertEqual(await self._count_stars(self.user_a_id), 1)

        async with self.session_factory() as session:
            result = await toggle_channel_star(
                account_id=self.account_id,
                channel_id="103",
                auth=_make_auth(self.user_a_id),
                session=session,
            )
        self.assertEqual(result, {"starred": False})
        self.assertEqual(await self._count_stars(self.user_a_id), 0)

    async def test_toggle_star_unknown_account_404(self):
        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await toggle_channel_star(
                    account_id=9999,
                    channel_id="103",
                    auth=_make_auth(self.user_a_id),
                    session=session,
                )
        self.assertEqual(ctx.exception.status_code, 404)

    # ------------------------------------------------------------------
    # per-user isolation
    # ------------------------------------------------------------------

    async def test_star_is_isolated_per_user(self):
        """User A's star must not appear in user B's channel list."""
        async with self.session_factory() as session:
            await toggle_channel_star(
                account_id=self.account_id,
                channel_id="102",
                auth=_make_auth(self.user_a_id),
                session=session,
            )

        channels_a = await self._get_channels(self.user_a_id)
        channels_b = await self._get_channels(self.user_b_id)

        starred_a = {ch["stream_id"] for ch in channels_a if ch["starred"]}
        starred_b = {ch["stream_id"] for ch in channels_b if ch["starred"]}
        self.assertEqual(starred_a, {102}, "User A must see their starred channel flagged.")
        self.assertEqual(starred_b, set(), "User B must not inherit user A's star.")

    async def test_unstar_does_not_touch_other_users_star(self):
        """Both users star the same channel; unstarring as A keeps B's star."""
        for user_id in (self.user_a_id, self.user_b_id):
            async with self.session_factory() as session:
                await toggle_channel_star(
                    account_id=self.account_id,
                    channel_id="101",
                    auth=_make_auth(user_id),
                    session=session,
                )

        async with self.session_factory() as session:
            await toggle_channel_star(
                account_id=self.account_id,
                channel_id="101",
                auth=_make_auth(self.user_a_id),
                session=session,
            )

        self.assertEqual(await self._count_stars(self.user_a_id), 0)
        self.assertEqual(await self._count_stars(self.user_b_id), 1)

    # ------------------------------------------------------------------
    # ordering
    # ------------------------------------------------------------------

    async def test_starred_channels_sort_first(self):
        """A starred channel floats to the top; the rest keep provider order."""
        async with self.session_factory() as session:
            await toggle_channel_star(
                account_id=self.account_id,
                channel_id="103",
                auth=_make_auth(self.user_a_id),
                session=session,
            )

        channels = await self._get_channels(self.user_a_id)
        self.assertEqual(
            [ch["stream_id"] for ch in channels],
            [103, 101, 102],
            "Starred channel must sort first; unstarred channels keep provider order.",
        )
        self.assertTrue(channels[0]["starred"])

    async def test_no_stars_keeps_provider_order(self):
        channels = await self._get_channels(self.user_b_id)
        self.assertEqual([ch["stream_id"] for ch in channels], [101, 102, 103])
        self.assertTrue(all(ch["starred"] is False for ch in channels))


class StarredChannelWiringTests(unittest.TestCase):

    def test_toggle_star_requires_authenticated_user(self):
        """toggle_channel_star must use require_admin_or_download_user (download_only users can star)."""
        sig = inspect.signature(toggle_channel_star)
        auth_param = sig.parameters.get("auth")
        self.assertIsNotNone(auth_param, "auth parameter missing from handler")
        self.assertEqual(auth_param.default.dependency, require_admin_or_download_user)

    def test_get_channels_receives_auth_context(self):
        """get_channels must depend on require_admin_or_download_user to resolve the user's stars."""
        sig = inspect.signature(get_channels)
        auth_param = sig.parameters.get("auth")
        self.assertIsNotNone(auth_param, "auth parameter missing from handler")
        self.assertEqual(auth_param.default.dependency, require_admin_or_download_user)


if __name__ == "__main__":
    unittest.main()
