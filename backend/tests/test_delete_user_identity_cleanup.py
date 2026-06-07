"""
Regression test: delete_user must remove associated UserIdentity rows.

Before fix: DELETE /admin/users/{id} deleted the User row but left any linked
UserIdentity rows intact. On the next Plex login, plex_login_complete found the
orphaned identity, tried to load the now-missing user, got None, and raised
HTTP 500 ("Plex identity is mapped to a missing user"). The Plex user was
permanently locked out with no way to recover short of direct DB surgery.

After fix: delete_user executes DELETE FROM user_identities WHERE user_id = <id>
before removing the User row, so stale identities cannot accumulate.

The real-DB tests below verify the WHERE clause actually targets the correct
user_id, not all rows. Creating two users (A and B) with separate identities and
deleting only A confirms that B's identity survives.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.admin_users import delete_user
from database import Base
from models import User, UserIdentity, UserSetupToken


def _make_user(display_name="Plex User"):
    return User(
        role="download_only",
        status="active",
        display_name=display_name,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_identity(user_id, subject):
    return UserIdentity(
        user_id=user_id,
        provider="plex",
        provider_subject=subject,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class DeleteUserIdentityCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed(self):
        """Insert user A and user B, each with one UserIdentity. Return (a_id, b_id)."""
        async with self.session_factory() as session:
            user_a = _make_user("User A")
            user_b = _make_user("User B")
            session.add_all([user_a, user_b])
            await session.commit()
            await session.refresh(user_a)
            await session.refresh(user_b)
            a_id, b_id = user_a.id, user_b.id

        async with self.session_factory() as session:
            session.add_all([
                _make_identity(a_id, "plex-subject-a"),
                _make_identity(b_id, "plex-subject-b"),
            ])
            await session.commit()

        return a_id, b_id

    async def _count_identities(self, user_id):
        async with self.session_factory() as session:
            result = await session.execute(
                select(UserIdentity).where(UserIdentity.user_id == user_id)
            )
            return len(result.scalars().all())

    async def test_deleted_users_identity_is_removed(self):
        """UserIdentity row for deleted user A must be gone after delete_user."""
        a_id, _ = await self._seed()
        async with self.session_factory() as session:
            await delete_user(a_id, _admin=None, session=session)
        self.assertEqual(
            await self._count_identities(a_id),
            0,
            "UserIdentity rows for deleted user A were not removed.",
        )

    async def test_other_users_identity_survives(self):
        """UserIdentity row for unrelated user B must not be touched."""
        a_id, b_id = await self._seed()
        async with self.session_factory() as session:
            await delete_user(a_id, _admin=None, session=session)
        self.assertEqual(
            await self._count_identities(b_id),
            1,
            "UserIdentity row for user B was deleted when only user A was removed.",
        )

    async def test_delete_user_returns_deleted_status(self):
        a_id, _ = await self._seed()
        async with self.session_factory() as session:
            result = await delete_user(a_id, _admin=None, session=session)
        self.assertEqual(result, {"status": "deleted"})

    async def test_user_row_is_removed(self):
        a_id, _ = await self._seed()
        async with self.session_factory() as session:
            await delete_user(a_id, _admin=None, session=session)
        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.id == a_id))
            self.assertIsNone(result.scalar_one_or_none(), "User row was not deleted.")

    async def test_user_not_found_raises_404(self):
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=type("R", (), {"scalar_one_or_none": lambda self: None})()
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delete_user(99, _admin=None, session=session)
        self.assertEqual(ctx.exception.status_code, 404)


class DeleteUserSetupTokenCleanupTests(unittest.IsolatedAsyncioTestCase):
    """UserSetupToken rows must also be removed when a user is deleted."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_with_token(self):
        async with self.session_factory() as session:
            user = _make_user("Token User")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            uid = user.id

        async with self.session_factory() as session:
            token = UserSetupToken(
                user_id=uid,
                token_hash="deadbeef" * 8,
                expires_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            session.add(token)
            await session.commit()

        return uid

    async def test_setup_token_removed_on_user_delete(self):
        uid = await self._seed_with_token()
        async with self.session_factory() as session:
            await delete_user(uid, _admin=None, session=session)
        async with self.session_factory() as session:
            result = await session.execute(
                select(UserSetupToken).where(UserSetupToken.user_id == uid)
            )
            rows = result.scalars().all()
        self.assertEqual(
            len(rows),
            0,
            "UserSetupToken rows were not removed when the user was deleted.",
        )


if __name__ == "__main__":
    unittest.main()
