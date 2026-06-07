"""
Regression test: complete_user_setup must not reactivate disabled users.

Before fix: POST /api/auth/user/setup/{token} set user.status = "active"
unconditionally. An admin who disabled a user could have their decision bypassed
if an unexpired setup link existed: the user could POST the token and reactivate
themselves within the 24-hour TTL.

generate_setup_link also had no status check, so an admin could accidentally
issue a link for an already-disabled user, granting them a reactivation window.

After fix:
- complete_user_setup returns HTTP 403 if the user is disabled.
- generate_setup_link returns HTTP 400 if the user is disabled.
- Active and pending users are unaffected.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select

from api.auth import complete_user_setup, UserSetupPayload, _hash_setup_token
from api.admin_users import generate_setup_link
from database import Base
from models import User, UserSetupToken


def _make_user(status="pending"):
    return User(
        role="download_only",
        status=status,
        display_name="Test User",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_token(user_id, raw_token="test-token-abc", hours_ahead=24):
    return UserSetupToken(
        user_id=user_id,
        token_hash=_hash_setup_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(hours=hours_ahead),
        created_at=datetime.now(timezone.utc),
    )


class CompleteSetupDisabledUserTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed(self, status="pending", raw_token="test-token-abc"):
        async with self.session_factory() as session:
            user = _make_user(status=status)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            uid = user.id

        async with self.session_factory() as session:
            token = _make_token(uid, raw_token=raw_token)
            session.add(token)
            await session.commit()

        return uid

    async def test_disabled_user_gets_403(self):
        """complete_user_setup must return 403 when user is disabled."""
        raw = "test-token-disabled"
        await self._seed(status="disabled", raw_token=raw)

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await complete_user_setup(
                    raw, UserSetupPayload(password="Password1!"), session
                )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_disabled_user_status_unchanged(self):
        """complete_user_setup must not change a disabled user's status to active."""
        raw = "test-token-disabled2"
        uid = await self._seed(status="disabled", raw_token=raw)

        async with self.session_factory() as session:
            try:
                await complete_user_setup(
                    raw, UserSetupPayload(password="Password1!"), session
                )
            except HTTPException:
                pass

        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.id == uid))
            user = result.scalar_one_or_none()
        self.assertEqual(user.status, "disabled", "Disabled user was reactivated via setup link.")

    async def test_pending_user_is_activated(self):
        """complete_user_setup must still activate a pending user (regression guard)."""
        raw = "test-token-pending"
        uid = await self._seed(status="pending", raw_token=raw)

        async with self.session_factory() as session:
            result = await complete_user_setup(
                raw, UserSetupPayload(password="Password1!"), session
            )
        self.assertEqual(result, {"status": "configured"})

        async with self.session_factory() as session:
            row = await session.execute(select(User).where(User.id == uid))
            user = row.scalar_one_or_none()
        self.assertEqual(user.status, "active")

    async def test_active_user_remains_active(self):
        """complete_user_setup (e.g. for password reset) does not break active users."""
        raw = "test-token-active"
        uid = await self._seed(status="active", raw_token=raw)

        async with self.session_factory() as session:
            result = await complete_user_setup(
                raw, UserSetupPayload(password="Password1!"), session
            )
        self.assertEqual(result, {"status": "configured"})

        async with self.session_factory() as session:
            row = await session.execute(select(User).where(User.id == uid))
            user = row.scalar_one_or_none()
        self.assertEqual(user.status, "active")


class GenerateSetupLinkDisabledUserTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _create_user(self, status="active"):
        async with self.session_factory() as session:
            user = _make_user(status=status)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user.id

    async def test_disabled_user_returns_400(self):
        """generate_setup_link must return 400 for a disabled user."""
        uid = await self._create_user(status="disabled")
        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await generate_setup_link(uid, _admin=None, session=session)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_active_user_gets_link(self):
        """generate_setup_link must still work for an active user (regression guard)."""
        uid = await self._create_user(status="active")
        async with self.session_factory() as session:
            result = await generate_setup_link(uid, _admin=None, session=session)
        self.assertIn("token", result)
        self.assertIn("setup_path", result)


if __name__ == "__main__":
    unittest.main()
