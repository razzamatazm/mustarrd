"""
Regression tests for DELETE /api/downloads/finished.

The endpoint removes all completed and failed download entries for the
requesting user and returns {"deleted": N}. It never touches downloads
that are downloading, pending, processing, or cancelled.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import Download, DownloadStatus


class ClearFinishedTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_download(self, status, user_id=1):
        d = MagicMock(spec=Download)
        d.status = status
        d.requested_by_user_id = user_id
        return d

    def _make_session(self, rowcount):
        execute_result = MagicMock()
        execute_result.rowcount = rowcount
        session = AsyncMock()
        session.execute = AsyncMock(return_value=execute_result)
        session.commit = AsyncMock()
        return session

    def _make_admin_auth(self):
        auth = MagicMock()
        auth.is_admin = True
        auth.user_id = 1
        return auth

    def _make_user_auth(self, user_id=2):
        auth = MagicMock()
        auth.is_admin = False
        auth.user_id = user_id
        return auth

    def test_admin_clears_all_completed_and_failed(self):
        from api.downloads import clear_finished_downloads
        session = self._make_session(rowcount=5)
        auth = self._make_admin_auth()
        result = self._run(clear_finished_downloads(auth=auth, session=session))
        self.assertEqual(result["deleted"], 5)
        session.commit.assert_awaited_once()

    def test_returns_zero_when_nothing_to_clear(self):
        from api.downloads import clear_finished_downloads
        session = self._make_session(rowcount=0)
        auth = self._make_admin_auth()
        result = self._run(clear_finished_downloads(auth=auth, session=session))
        self.assertEqual(result["deleted"], 0)

    def test_non_admin_scoped_to_own_downloads(self):
        """Non-admin request must add a user_id filter to the delete query."""
        from api.downloads import clear_finished_downloads
        from sqlalchemy import delete as sql_delete

        captured_query = {}

        async def capture_execute(query, *args, **kwargs):
            captured_query["stmt"] = query
            result = MagicMock()
            result.rowcount = 2
            return result

        session = AsyncMock()
        session.execute = capture_execute
        session.commit = AsyncMock()
        auth = self._make_user_auth(user_id=42)

        result = self._run(clear_finished_downloads(auth=auth, session=session))
        self.assertEqual(result["deleted"], 2)

        stmt = captured_query.get("stmt")
        self.assertIsNotNone(stmt, "execute was not called")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("42", compiled, "user_id filter must be present for non-admin")

    def test_only_completed_and_failed_statuses_targeted(self):
        """Delete statement must target only completed and failed, not pending/cancelled."""
        from api.downloads import clear_finished_downloads

        captured_query = {}

        async def capture_execute(query, *args, **kwargs):
            captured_query["stmt"] = query
            result = MagicMock()
            result.rowcount = 0
            return result

        session = AsyncMock()
        session.execute = capture_execute
        session.commit = AsyncMock()
        auth = self._make_admin_auth()

        self._run(clear_finished_downloads(auth=auth, session=session))

        stmt = captured_query.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn(DownloadStatus.COMPLETED.value, compiled)
        self.assertIn(DownloadStatus.FAILED.value, compiled)
        self.assertNotIn(DownloadStatus.PENDING.value, compiled)
        self.assertNotIn(DownloadStatus.DOWNLOADING.value, compiled)
        self.assertNotIn(DownloadStatus.CANCELLED.value, compiled)


if __name__ == "__main__":
    unittest.main()
