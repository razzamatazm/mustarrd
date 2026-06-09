"""
Regression tests: PlexServer.enabled=False must block all Plex login flows.

Before fix: the `enabled` column had no effect. An admin who set enabled=False
via PUT /api/admin/plex/integration could not actually block Plex logins.
plex_login_start, plex_login_complete, and auth_status all ignored the field.

After fix:
- plex_login_start returns HTTP 403 when enabled=False.
- plex_login_complete returns HTTP 403 when enabled=False.
- auth_status returns plex_login_available=False when enabled=False.
- All three work normally when enabled=True (regression guard).
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.auth import PlexCompletePayload, auth_status, plex_login_complete, plex_login_start
from database import Base
from models import PlexServer

PLEX_SUBJECT = "plex-subject-002"


def _mock_request():
    req = MagicMock()
    req.session = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = {}
    return req


def _plex_profile():
    return {"id": PLEX_SUBJECT, "username": "plexuser", "email": "plex@example.com"}


class PlexEnabledGuardTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_plex_server(self, enabled=True):
        async with self.session_factory() as session:
            server = PlexServer(
                base_url="http://plex.local",
                token_encrypted="enc-tok",
                access_token_encrypted="enc-tok",
                auto_allow_all_server_users=True,
                enabled=enabled,
            )
            session.add(server)
            await session.commit()

    # --- plex_login_start ---

    async def test_start_disabled_server_raises_403(self):
        """plex_login_start must return 403 when PlexServer.enabled=False."""
        await self._seed_plex_server(enabled=False)

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await plex_login_start(_mock_request(), session)

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_start_enabled_server_proceeds(self):
        """plex_login_start must call create_pin when PlexServer.enabled=True."""
        await self._seed_plex_server(enabled=True)

        create_pin_mock = AsyncMock(return_value={"id": 1, "code": "ABCD", "expires_in": 300})
        with patch("api.auth.plex_service.create_pin", create_pin_mock):
            async with self.session_factory() as session:
                result = await plex_login_start(_mock_request(), session)

        create_pin_mock.assert_called_once()
        self.assertIn("id", result)

    # --- plex_login_complete ---

    async def test_complete_disabled_server_raises_403(self):
        """plex_login_complete must return 403 when PlexServer.enabled=False."""
        await self._seed_plex_server(enabled=False)

        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())):
            async with self.session_factory() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await plex_login_complete(
                        PlexCompletePayload(pin_id=42),
                        _mock_request(),
                        session,
                    )

        self.assertEqual(ctx.exception.status_code, 403)

    async def test_complete_enabled_server_proceeds(self):
        """plex_login_complete must proceed past the guard when PlexServer.enabled=True."""
        await self._seed_plex_server(enabled=True)

        with patch("api.auth._enforce_rate_limit"), \
             patch("api.auth.plex_service.check_pin", new=AsyncMock(return_value={"authToken": "tok"})), \
             patch("api.auth.plex_service.get_user_profile", new=AsyncMock(return_value=_plex_profile())), \
             patch("api.auth.credential_crypto.decrypt", return_value="srv-tok"), \
             patch("api.auth.plex_service.user_is_in_server", new=AsyncMock(return_value=True)), \
             patch("api.auth.clear_auth_session"), \
             patch("api.auth.set_download_user_session"):
            async with self.session_factory() as session:
                result = await plex_login_complete(
                    PlexCompletePayload(pin_id=42),
                    _mock_request(),
                    session,
                )

        self.assertEqual(result["status"], "authenticated")

    # --- auth_status plex_login_available ---

    async def test_auth_status_plex_login_available_false_when_disabled(self):
        """auth_status must report plex_login_available=False when PlexServer.enabled=False."""
        await self._seed_plex_server(enabled=False)

        mock_context = MagicMock()
        mock_context.authenticated = False
        mock_context.user = None
        mock_context.role = None
        mock_context.provider = None
        mock_context.is_admin = False

        mock_settings = MagicMock()
        mock_settings.admin_password_hash = None
        mock_settings.admin_username_bootstrap_required = False
        mock_settings.show_future_programs = False

        with patch("api.auth.get_auth_context", new=AsyncMock(return_value=mock_context)), \
             patch("api.auth.get_or_create_app_settings", new=AsyncMock(return_value=mock_settings)), \
             patch("api.auth._bootstrap_required", new=AsyncMock(return_value=False)):
            async with self.session_factory() as session:
                result = await auth_status(_mock_request(), session)

        self.assertFalse(result["plex_login_available"])

    async def test_auth_status_plex_login_available_true_when_enabled(self):
        """auth_status must report plex_login_available=True when PlexServer.enabled=True and fully configured."""
        await self._seed_plex_server(enabled=True)

        mock_context = MagicMock()
        mock_context.authenticated = False
        mock_context.user = None
        mock_context.role = None
        mock_context.provider = None
        mock_context.is_admin = False

        mock_settings = MagicMock()
        mock_settings.admin_password_hash = None
        mock_settings.admin_username_bootstrap_required = False
        mock_settings.show_future_programs = False

        with patch("api.auth.get_auth_context", new=AsyncMock(return_value=mock_context)), \
             patch("api.auth.get_or_create_app_settings", new=AsyncMock(return_value=mock_settings)), \
             patch("api.auth._bootstrap_required", new=AsyncMock(return_value=False)):
            async with self.session_factory() as session:
                result = await auth_status(_mock_request(), session)

        self.assertTrue(result["plex_login_available"])


if __name__ == "__main__":
    unittest.main()
