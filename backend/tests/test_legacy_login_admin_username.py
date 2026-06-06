import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from api.auth import login_auth_legacy, PasswordPayload


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_request():
    return MagicMock()


class LegacyLoginAdminUsernameTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_admin_username_passed_to_login_credentials(self):
        """Admin with username 'tyler' can log in via the legacy endpoint."""
        payload = PasswordPayload(password="Test1234!")
        request = _make_request()
        session = MagicMock()

        admin_user = _make_user("tyler")

        with (
            patch("api.auth.get_admin_user", new=AsyncMock(return_value=admin_user)),
            patch("api.auth.login_credentials", new=AsyncMock(return_value={"status": "authenticated"})) as mock_login,
        ):
            result = await login_auth_legacy(payload, request, session)

        self.assertEqual(result["status"], "authenticated")
        call_payload = mock_login.call_args[0][0]
        self.assertEqual(call_payload.username, "tyler")
        self.assertEqual(call_payload.password, "Test1234!")

    async def test_no_admin_user_returns_401(self):
        """Legacy endpoint returns 401 when no admin user exists."""
        payload = PasswordPayload(password="Test1234!")
        request = _make_request()
        session = MagicMock()

        with patch("api.auth.get_admin_user", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as ctx:
                await login_auth_legacy(payload, request, session)

        self.assertEqual(ctx.exception.status_code, 401)

    async def test_default_admin_username_still_works(self):
        """Admin with the default username 'admin' continues to work."""
        payload = PasswordPayload(password="Test1234!")
        request = _make_request()
        session = MagicMock()

        admin_user = _make_user("admin")

        with (
            patch("api.auth.get_admin_user", new=AsyncMock(return_value=admin_user)),
            patch("api.auth.login_credentials", new=AsyncMock(return_value={"status": "authenticated"})) as mock_login,
        ):
            result = await login_auth_legacy(payload, request, session)

        call_payload = mock_login.call_args[0][0]
        self.assertEqual(call_payload.username, "admin")


if __name__ == "__main__":
    unittest.main()
