"""
Regression test: get_auth_context must verify User.role == "download_only" in DB.

Before fix: the download_only branch queried only by User.id, never checking
User.role. A session cookie with auth_role="download_only" for an admin user_id
was granted download_only access without DB role confirmation.

After fix: the query filters on both User.id AND User.role == "download_only",
consistent with how the admin branch already works. An admin user with a tampered
download_only cookie gets AuthContext(authenticated=False).
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from auth import SESSION_AUTH_ROLE_KEY, SESSION_USER_ID_KEY, get_auth_context
from database import Base
from models import User


def _mock_request(role, user_id):
    req = MagicMock()
    req.session = {SESSION_AUTH_ROLE_KEY: role, SESSION_USER_ID_KEY: user_id}
    return req


class AuthContextRoleCheckTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _create_user(self, role):
        async with self.Session() as session:
            user = User(
                role=role,
                status="active",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(user)
            await session.commit()
            return user.id

    async def test_download_only_user_authenticates(self):
        """download_only cookie for download_only DB user authenticates correctly."""
        uid = await self._create_user("download_only")
        async with self.Session() as session:
            ctx = await get_auth_context(_mock_request("download_only", uid), session)
        self.assertTrue(ctx.authenticated)
        self.assertEqual(ctx.role, "download_only")

    async def test_admin_user_with_download_only_cookie_rejected(self):
        """Admin DB user with a tampered download_only cookie must not authenticate.

        Before fix: get_auth_context queried only by User.id and granted access.
        After fix: the query also filters by User.role == "download_only", so the
        admin row is not returned and the session is rejected.
        """
        uid = await self._create_user("admin")
        async with self.Session() as session:
            ctx = await get_auth_context(_mock_request("download_only", uid), session)
        self.assertFalse(ctx.authenticated)

    async def test_unknown_user_id_rejected(self):
        """Non-existent user_id in download_only cookie must not authenticate."""
        async with self.Session() as session:
            ctx = await get_auth_context(_mock_request("download_only", 9999), session)
        self.assertFalse(ctx.authenticated)


if __name__ == "__main__":
    unittest.main()
