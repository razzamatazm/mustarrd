"""
Regression test: plex_login_complete must not reactivate disabled users.

Before fix: POST /api/auth/plex/login/complete called user.status = "active"
unconditionally for existing Plex-linked users. An admin who disabled a Plex
user had no effective way to block them: re-completing the Plex PIN flow
overwrote the disabled status and granted a valid session.

After fix:
- plex_login_complete returns HTTP 403 if the existing linked user is disabled.
- Disabled user's status remains "disabled" after the attempt.
- Active and new Plex users are unaffected.
"""
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

from api.auth import PlexCompletePayload, plex_login_complete
from database import Base
from models import PlexServer, User, UserIdentity

PLEX_SUBJECT = "plex-subject-001"


def _mock_request():
    req = MagicMock()
    req.session = {}
    return req


def _plex_profile():
    return {"id": PLEX_SUBJECT, "username": "testplexuser", "email": "plex@example.com"}


class PlexLoginDisabledUserTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_plex_server(self):
        async with self.session_factory() as session:
            server = PlexServer(
                base_url="http://plex.local",
                token_encrypted="enc-tok",
                access_token_encrypted="enc-tok",
                auto_allow_all_server_users=True,
            )
            session.add(server)
            await session.commit()

    async def _seed_linked_user(self, status="active"):
        async with self.session_factory() as session:
            user = User(
                role="download_only",
                status=status,
                display_name="Plex User",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(user)
            await session.flush()
            identity = UserIdentity(
                user_id=user.id,
                provider="plex",
                provider_subject=PLEX_SUBJECT,
                provider_username="testplexuser",
                provider_email="plex@example.com",
            )
            session.add(identity)
            await session.commit()
            return user.id

    async def _call(self, session):
        return await plex_login_complete(
            PlexCompletePayload(pin_id=42),
            _mock_request(),
            session,
        )

    async def test_disabled_user_gets_403(self):
        """Existing disabled Plex-linked user must receive HTTP 403."""
        await self._seed_plex_server()
        uid = await self._seed_linked_user(status="disabled")

        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())), \
             patch("api.auth.plex_service.user_is_in_server", new=AsyncMock(return_value=True)), \
             patch("api.auth.credential_crypto.decrypt", return_value="srv-tok"), \
             patch("api.auth.clear_auth_session"), \
             patch("api.auth.set_download_user_session"):
            async with self.session_factory() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await self._call(session)

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_disabled_user_status_unchanged(self):
        """Disabled user's status must remain 'disabled' after the attempt."""
        await self._seed_plex_server()
        uid = await self._seed_linked_user(status="disabled")

        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())), \
             patch("api.auth.plex_service.user_is_in_server", new=AsyncMock(return_value=True)), \
             patch("api.auth.credential_crypto.decrypt", return_value="srv-tok"), \
             patch("api.auth.clear_auth_session"), \
             patch("api.auth.set_download_user_session"):
            async with self.session_factory() as session:
                try:
                    await self._call(session)
                except HTTPException:
                    pass

        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.id == uid))
            user = result.scalar_one_or_none()
        self.assertEqual(user.status, "disabled", "Disabled Plex user was reactivated.")

    async def test_active_user_succeeds(self):
        """Existing active Plex-linked user must still get a valid session."""
        await self._seed_plex_server()
        uid = await self._seed_linked_user(status="active")

        set_session_mock = MagicMock()
        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())), \
             patch("api.auth.plex_service.user_is_in_server", new=AsyncMock(return_value=True)), \
             patch("api.auth.credential_crypto.decrypt", return_value="srv-tok"), \
             patch("api.auth.clear_auth_session"), \
             patch("api.auth.set_download_user_session", set_session_mock):
            async with self.session_factory() as session:
                result = await self._call(session)

        self.assertEqual(result["status"], "authenticated")
        set_session_mock.assert_called_once()

    async def test_active_user_status_stays_active(self):
        """Active user status must remain 'active' after login (regression guard)."""
        await self._seed_plex_server()
        uid = await self._seed_linked_user(status="active")

        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())), \
             patch("api.auth.plex_service.user_is_in_server", new=AsyncMock(return_value=True)), \
             patch("api.auth.credential_crypto.decrypt", return_value="srv-tok"), \
             patch("api.auth.clear_auth_session"), \
             patch("api.auth.set_download_user_session"):
            async with self.session_factory() as session:
                await self._call(session)

        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.id == uid))
            user = result.scalar_one_or_none()
        self.assertEqual(user.status, "active")


if __name__ == "__main__":
    unittest.main()
