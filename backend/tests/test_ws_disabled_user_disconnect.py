"""
Regression test for: disabled user keeps WebSocket connection open after admin disables them.

Root cause: download_manager.register_websocket() stores auth at connection time only.
PATCH /admin/users/{id} sets user.status="disabled" but never closes open WS connections
for that user. The disabled user continues to receive real-time download progress events.

Fix: download_manager.disconnect_user_websockets(user_id) closes and removes all WS
connections for a given user. Called from update_user after status is set to "disabled".
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


def _make_ws(user_id, is_admin=False):
    ws = AsyncMock()
    ws.close = AsyncMock()
    return ws, {"user_id": user_id, "is_admin": is_admin}


class DisconnectUserWebsocketsTests(unittest.IsolatedAsyncioTestCase):

    async def test_disconnect_closes_matching_ws(self):
        """disconnect_user_websockets closes the socket for the given user."""
        dm = DownloadManager()
        ws, auth = _make_ws(user_id=5)
        dm._websocket_connections[ws] = auth

        await dm.disconnect_user_websockets(5)

        ws.close.assert_awaited_once()

    async def test_disconnect_removes_from_connections(self):
        """After disconnect, the WS is removed from _websocket_connections."""
        dm = DownloadManager()
        ws, auth = _make_ws(user_id=5)
        dm._websocket_connections[ws] = auth

        await dm.disconnect_user_websockets(5)

        self.assertNotIn(ws, dm._websocket_connections)

    async def test_disconnect_leaves_other_users_alone(self):
        """Connections for other users are not closed."""
        dm = DownloadManager()
        ws_target, auth_target = _make_ws(user_id=5)
        ws_other, auth_other = _make_ws(user_id=7)
        dm._websocket_connections[ws_target] = auth_target
        dm._websocket_connections[ws_other] = auth_other

        await dm.disconnect_user_websockets(5)

        ws_other.close.assert_not_awaited()
        self.assertIn(ws_other, dm._websocket_connections)

    async def test_disconnect_tolerates_close_error(self):
        """If ws.close() raises, disconnect continues without propagating the error."""
        dm = DownloadManager()
        ws, auth = _make_ws(user_id=5)
        ws.close.side_effect = Exception("already closed")
        dm._websocket_connections[ws] = auth

        await dm.disconnect_user_websockets(5)

        self.assertNotIn(ws, dm._websocket_connections)

    async def test_disconnect_no_connections_is_noop(self):
        """Calling disconnect when no connections exist does not raise."""
        dm = DownloadManager()
        await dm.disconnect_user_websockets(99)

    async def test_update_user_calls_disconnect_on_disable(self):
        """PATCH /admin/users/{id} with status=disabled triggers disconnect_user_websockets."""
        from api.admin_users import update_user, UpdateUserPayload

        user = MagicMock()
        user.id = 3
        user.role = "download_only"
        user.status = "active"
        user.display_name = "Alice"
        user.password_hash = None
        user.username = "alice"
        user.to_dict.return_value = {"id": 3, "status": "disabled"}

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = user

        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        admin_auth = MagicMock()
        admin_auth.is_admin = True

        with patch("api.admin_users.download_manager") as mock_dm:
            mock_dm.disconnect_user_websockets = AsyncMock()
            payload = UpdateUserPayload(status="disabled")
            await update_user(user_id=3, payload=payload, _admin=admin_auth, session=session)

        mock_dm.disconnect_user_websockets.assert_awaited_once_with(3)

    async def test_update_user_no_disconnect_on_reactivate(self):
        """PATCH /admin/users/{id} with status=active does NOT call disconnect_user_websockets."""
        from api.admin_users import update_user, UpdateUserPayload

        user = MagicMock()
        user.id = 4
        user.role = "download_only"
        user.status = "disabled"
        user.display_name = "Bob"
        user.password_hash = None
        user.username = "bob"
        user.to_dict.return_value = {"id": 4, "status": "active"}

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = user

        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        admin_auth = MagicMock()
        admin_auth.is_admin = True

        with patch("api.admin_users.download_manager") as mock_dm:
            mock_dm.disconnect_user_websockets = AsyncMock()
            payload = UpdateUserPayload(status="active")
            await update_user(user_id=4, payload=payload, _admin=admin_auth, session=session)

        mock_dm.disconnect_user_websockets.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
