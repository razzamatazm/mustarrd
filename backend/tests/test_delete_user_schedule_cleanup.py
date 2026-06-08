"""
Regression tests: delete_user must cancel non-terminal scheduled recordings
and active downloads before removing the user row, and must disconnect any
open WebSocket connections for that user.

Before fix: session.delete(user) leaves ScheduledRecording and Download rows
with requested_by_user_id pointing at the deleted user.  On the next scheduler
poll, those orphaned schedules fire normally and create downloads attributed to
a ghost user.  Active in-progress downloads continue running after deletion.
WebSocket connections from the deleted user are not closed.

After fix: non-terminal scheduled recordings (SCHEDULED, QUEUED, DOWNLOADING,
PROCESSING, PAUSED_LOW_SPACE) are marked CANCELLED with status_message "User
deleted" before the user row is removed.  Active downloads (PENDING,
DOWNLOADING, PROCESSING) are passed to download_manager.cancel_download.
download_manager.disconnect_user_websockets is called for the user.

File:     backend/api/admin_users.py, delete_user
Severity: MEDIUM -- orphaned schedules silently fire on behalf of deleted
          users, consuming disk space and IPTV stream slots; in-progress
          downloads run unrestricted after user removal; live WS connections
          are not revoked.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.admin_users import delete_user
from models import Download, DownloadStatus, ScheduledRecording, ScheduledStatus, User


def _scalar_one(value):
    class R:
        def scalar_one_or_none(self):
            return value
    return R()


def _scalar_list(items):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))


def _make_user(user_id=1):
    u = User()
    u.id = user_id
    u.role = "download_only"
    u.username = "testuser"
    u.status = "active"
    return u


def _make_schedule(sched_id, status, user_id=1):
    s = ScheduledRecording()
    s.id = sched_id
    s.account_id = 1
    s.channel_id = "ch1"
    s.channel_name = "Channel 1"
    s.program_title = "Show"
    s.program_start = None
    s.program_end = None
    s.duration_minutes = 60
    s.status = status
    s.status_message = None
    s.requested_by_user_id = user_id
    s.download_id = None
    return s


def _make_download(dl_id, status, user_id=1):
    d = Download()
    d.id = dl_id
    d.account_id = 1
    d.status = status
    d.requested_by_user_id = user_id
    return d


class DeleteUserScheduleCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def _run_delete(self, user, schedules, active_downloads):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _scalar_one(user),            # select(User)
            AsyncMock(),                  # delete(UserIdentity)
            AsyncMock(),                  # delete(UserSetupToken)
            _scalar_list(schedules),      # select(ScheduledRecording) -- added by fix
            _scalar_list(active_downloads),  # select(Download) -- added by fix
        ])

        with patch("api.admin_users.download_manager") as mock_dm:
            mock_dm.cancel_download = AsyncMock()
            mock_dm.disconnect_user_websockets = AsyncMock()
            result = await delete_user(user.id, None, session)

        return session, mock_dm, result

    async def test_active_schedule_marked_cancelled_on_user_delete(self):
        """Active schedule must be CANCELLED when the owning user is deleted."""
        sched = _make_schedule(10, ScheduledStatus.SCHEDULED.value)
        session, _, result = await self._run_delete(_make_user(), [sched], [])
        self.assertEqual(sched.status, ScheduledStatus.CANCELLED.value)
        self.assertEqual(sched.status_message, "User deleted")
        self.assertEqual(result, {"status": "deleted"})

    async def test_all_non_terminal_statuses_are_cancelled(self):
        non_terminal = [
            ScheduledStatus.SCHEDULED,
            ScheduledStatus.QUEUED,
            ScheduledStatus.DOWNLOADING,
            ScheduledStatus.PROCESSING,
            ScheduledStatus.PAUSED_LOW_SPACE,
        ]
        for status in non_terminal:
            with self.subTest(status=status.value):
                sched = _make_schedule(1, status.value)
                await self._run_delete(_make_user(), [sched], [])
                self.assertEqual(sched.status, ScheduledStatus.CANCELLED.value)
                self.assertEqual(sched.status_message, "User deleted")

    async def test_active_download_triggers_cancel(self):
        """Active download must be cancelled when the owning user is deleted."""
        dl = _make_download(42, DownloadStatus.DOWNLOADING.value)
        _, mock_dm, _ = await self._run_delete(_make_user(), [], [dl])
        mock_dm.cancel_download.assert_awaited_once_with(42)

    async def test_multiple_active_downloads_all_cancelled(self):
        downloads = [
            _make_download(1, DownloadStatus.PENDING.value),
            _make_download(2, DownloadStatus.DOWNLOADING.value),
            _make_download(3, DownloadStatus.PROCESSING.value),
        ]
        _, mock_dm, _ = await self._run_delete(_make_user(), [], downloads)
        called_ids = [call.args[0] for call in mock_dm.cancel_download.await_args_list]
        self.assertCountEqual(called_ids, [1, 2, 3])

    async def test_websockets_disconnected_on_user_delete(self):
        """Open WebSocket connections for the deleted user must be closed."""
        _, mock_dm, _ = await self._run_delete(_make_user(user_id=7), [], [])
        mock_dm.disconnect_user_websockets.assert_awaited_once_with(7)

    async def test_no_active_work_no_cancel_called(self):
        _, mock_dm, _ = await self._run_delete(_make_user(), [], [])
        mock_dm.cancel_download.assert_not_awaited()

    async def test_session_delete_and_commit_called(self):
        user = _make_user()
        session, _, _ = await self._run_delete(user, [], [])
        session.delete.assert_awaited_with(user)
        session.commit.assert_awaited()

    async def test_commit_before_cancel_download(self):
        """session.commit must happen before cancel_download to avoid SQLite lock."""
        user = _make_user()
        dl = _make_download(42, DownloadStatus.DOWNLOADING.value)
        call_order = []

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _scalar_one(user),
            AsyncMock(),
            AsyncMock(),
            _scalar_list([]),
            _scalar_list([dl]),
        ])

        async def tracked_commit():
            call_order.append("commit")
        session.commit.side_effect = tracked_commit

        with patch("api.admin_users.download_manager") as mock_dm:
            async def tracked_cancel(dl_id):
                call_order.append(f"cancel:{dl_id}")
            mock_dm.cancel_download.side_effect = tracked_cancel
            mock_dm.disconnect_user_websockets = AsyncMock()
            await delete_user(user.id, None, session)

        self.assertIn("commit", call_order, "commit was never called")
        self.assertIn("cancel:42", call_order, "cancel_download was never called")
        commit_pos = call_order.index("commit")
        cancel_pos = call_order.index("cancel:42")
        self.assertLess(commit_pos, cancel_pos,
                        f"cancel_download called before commit: {call_order}")


if __name__ == "__main__":
    unittest.main()
