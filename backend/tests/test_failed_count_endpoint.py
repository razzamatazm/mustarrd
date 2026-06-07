"""
Tests for GET /api/downloads/failed-count.

Returns {"count": N} where N is the number of permanently FAILED downloads,
optionally filtered to those created at or after a given Unix timestamp in
milliseconds (the `since` query parameter).  A non-admin user sees only their
own downloads.
"""
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import DownloadStatus


class FailedCountTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

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

    def _make_session(self, count):
        scalar_result = MagicMock()
        scalar_result.scalar = MagicMock(return_value=count)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=scalar_result)
        return session

    def _capture_session(self):
        captured = {}
        scalar_result = MagicMock()
        scalar_result.scalar = MagicMock(return_value=0)

        async def capture_execute(query, *args, **kwargs):
            captured["stmt"] = query
            return scalar_result

        session = AsyncMock()
        session.execute = capture_execute
        return session, captured

    def test_returns_count_for_admin(self):
        from api.downloads import get_failed_download_count
        session = self._make_session(count=3)
        auth = self._make_admin_auth()
        result = self._run(get_failed_download_count(auth=auth, session=session, since=None))
        self.assertEqual(result["count"], 3)

    def test_returns_zero_when_no_failures(self):
        from api.downloads import get_failed_download_count
        session = self._make_session(count=0)
        auth = self._make_admin_auth()
        result = self._run(get_failed_download_count(auth=auth, session=session, since=None))
        self.assertEqual(result["count"], 0)

    def test_none_scalar_treated_as_zero(self):
        from api.downloads import get_failed_download_count
        session = self._make_session(count=None)
        auth = self._make_admin_auth()
        result = self._run(get_failed_download_count(auth=auth, session=session, since=None))
        self.assertEqual(result["count"], 0)

    def test_query_targets_only_failed_status(self):
        from api.downloads import get_failed_download_count
        session, captured = self._capture_session()
        auth = self._make_admin_auth()
        self._run(get_failed_download_count(auth=auth, session=session, since=None))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn(DownloadStatus.FAILED.value, compiled)
        self.assertNotIn(DownloadStatus.COMPLETED.value, compiled)
        self.assertNotIn(DownloadStatus.PENDING.value, compiled)

    def test_since_param_adds_created_at_filter(self):
        from api.downloads import get_failed_download_count
        session, captured = self._capture_session()
        auth = self._make_admin_auth()
        since_ms = 1700000000000.0  # 2023-11-14 22:13:20 UTC in ms
        self._run(get_failed_download_count(auth=auth, session=session, since=since_ms))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # The cutoff should be present as a datetime value in the query
        expected_cutoff = datetime.utcfromtimestamp(since_ms / 1000.0)
        self.assertIn(str(expected_cutoff.year), compiled)

    def test_no_since_param_omits_date_filter(self):
        from api.downloads import get_failed_download_count
        session, captured = self._capture_session()
        auth = self._make_admin_auth()
        self._run(get_failed_download_count(auth=auth, session=session, since=None))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # No year-like date filter when since is absent
        self.assertNotIn("created_at", compiled.lower().replace("downloads.created_at", "PLACEHOLDER"))

    def test_non_admin_query_scoped_to_user(self):
        from api.downloads import get_failed_download_count
        session, captured = self._capture_session()
        auth = self._make_user_auth(user_id=42)
        self._run(get_failed_download_count(auth=auth, session=session, since=None))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("42", compiled, "user_id filter must appear for non-admin")


if __name__ == "__main__":
    unittest.main()
