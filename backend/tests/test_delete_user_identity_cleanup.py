"""
Regression test: delete_user must remove associated UserIdentity rows.

Before fix: DELETE /admin/users/{id} deleted the User row but left any linked
UserIdentity rows intact. On the next Plex login, plex_login_complete found the
orphaned identity, tried to load the now-missing user, got None, and raised
HTTP 500 ("Plex identity is mapped to a missing user"). The Plex user was
permanently locked out with no way to recover short of direct DB surgery.

After fix: delete_user executes DELETE FROM user_identities WHERE user_id = <id>
before removing the User row, so stale identities cannot accumulate.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import delete as sa_delete

from api.admin_users import delete_user
from models import User, UserIdentity


def _scalar_one(value):
    class R:
        def scalar_one_or_none(self):
            return value
    return R()


def _make_user(user_id=7):
    u = User()
    u.id = user_id
    u.role = "download_only"
    u.status = "active"
    u.username = None
    u.display_name = "Plex User"
    return u


class DeleteUserIdentityCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def _run_delete(self, user):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _scalar_one(user),  # select(User).where(...)
            AsyncMock(),        # delete(UserIdentity).where(...)
        ])

        result = await delete_user(user.id, _admin=None, session=session)
        return session, result

    async def test_delete_user_returns_deleted_status(self):
        user = _make_user(7)
        _, result = await self._run_delete(user)
        self.assertEqual(result, {"status": "deleted"})

    async def test_user_identity_rows_deleted_before_user(self):
        """session.execute must be called twice: once for the user lookup and
        once for the UserIdentity delete. The delete must come before
        session.delete(user) so no orphaned identities remain."""
        user = _make_user(7)
        session, _ = await self._run_delete(user)

        # execute was called twice
        self.assertEqual(session.execute.call_count, 2)
        # session.delete was called with the user
        session.delete.assert_called_once_with(user)
        # session.commit was called once
        session.commit.assert_awaited_once()

    async def test_identity_delete_precedes_user_delete(self):
        """Verify the call order: execute (identity delete) then delete (user)."""
        user = _make_user(7)
        session, _ = await self._run_delete(user)

        execute_pos = None
        delete_pos = None
        for i, c in enumerate(session.mock_calls):
            name = c[0]
            if name == "execute" and execute_pos is None and i > 0:
                execute_pos = i
            elif name == "delete":
                delete_pos = i

        self.assertIsNotNone(execute_pos, "Second execute (identity delete) not found")
        self.assertIsNotNone(delete_pos, "session.delete(user) not found")
        self.assertLess(
            execute_pos,
            delete_pos,
            "UserIdentity delete must happen before session.delete(user); "
            "otherwise a crash mid-delete leaves orphaned identities.",
        )

    async def test_user_not_found_raises_404(self):
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_scalar_one(None))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delete_user(99, _admin=None, session=session)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
