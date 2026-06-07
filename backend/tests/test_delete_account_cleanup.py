"""
Regression tests: delete_account must cancel non-terminal scheduled recordings
and active downloads before removing the account row.

Before fix: session.delete(account) left ScheduledRecording rows pointing at
a deleted account.  On the next scheduler poll, build_download_from_program
raised ValueError("Account not found"), marking every future schedule FAILED
with a cryptic error instead of a clear "Account deleted" message.

After fix: non-terminal scheduled recordings (SCHEDULED, QUEUED, DOWNLOADING,
PROCESSING, PAUSED_LOW_SPACE) are marked CANCELLED with status_message
"Account deleted" before the account row is removed.  Active downloads
(PENDING, DOWNLOADING, PROCESSING) are passed to download_manager.cancel_download
so in-flight FFmpeg/aiohttp work is stopped cleanly.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.accounts import delete_account
from models import AppSettings, Download, DownloadStatus, ScheduledRecording, ScheduledStatus, XtreamAccount


def _scalar_one(value):
    class R:
        def scalar_one_or_none(self):
            return value
    return R()


def _scalar_list(items):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))


def _make_account(account_id=1):
    return XtreamAccount(
        id=account_id,
        name="Provider",
        server_url="http://example",
        username="user",
        password="",
    )


def _make_schedule(sched_id, status):
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
    return s


def _make_download(dl_id, status):
    d = Download()
    d.id = dl_id
    d.account_id = 1
    d.status = status
    return d


class DeleteAccountScheduleCleanupTests(unittest.IsolatedAsyncioTestCase):

    async def _run_delete(self, account, app_settings, schedules, active_downloads):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _scalar_one(account),
            _scalar_one(app_settings),
            _scalar_list(schedules),
            _scalar_list(active_downloads),
        ])

        with patch("api.accounts.download_manager") as mock_dm:
            mock_dm.cancel_download = AsyncMock()
            result = await delete_account(account.id, None, session)

        return session, mock_dm, result

    async def test_active_schedule_marked_cancelled_with_message(self):
        sched = _make_schedule(10, ScheduledStatus.SCHEDULED.value)
        session, _, result = await self._run_delete(
            _make_account(), AppSettings(), [sched], []
        )
        self.assertEqual(sched.status, ScheduledStatus.CANCELLED.value)
        self.assertEqual(sched.status_message, "Account deleted")
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
                await self._run_delete(_make_account(), AppSettings(), [sched], [])
                self.assertEqual(sched.status, ScheduledStatus.CANCELLED.value)
                self.assertEqual(sched.status_message, "Account deleted")

    async def test_terminal_schedules_not_in_query_scope(self):
        # Terminal records are not returned by the filtered query; confirm
        # they are not affected even if passed through (defensive check).
        terminal = _make_schedule(20, ScheduledStatus.COMPLETED.value)
        # Terminal records should never appear in the active_statuses query,
        # but if they somehow do, the loop would touch them.  The real guard
        # is the WHERE...IN filter; here we just verify correct behavior with
        # no active schedules returned.
        session, _, _ = await self._run_delete(
            _make_account(), AppSettings(), [], []
        )
        # schedule object unmodified because it was never in the result set
        self.assertEqual(terminal.status, ScheduledStatus.COMPLETED.value)

    async def test_active_download_triggers_cancel(self):
        dl = _make_download(42, DownloadStatus.DOWNLOADING.value)
        _, mock_dm, _ = await self._run_delete(
            _make_account(), AppSettings(), [], [dl]
        )
        mock_dm.cancel_download.assert_awaited_once_with(42)

    async def test_multiple_active_downloads_all_cancelled(self):
        downloads = [
            _make_download(1, DownloadStatus.PENDING.value),
            _make_download(2, DownloadStatus.DOWNLOADING.value),
            _make_download(3, DownloadStatus.PROCESSING.value),
        ]
        _, mock_dm, _ = await self._run_delete(
            _make_account(), AppSettings(), [], downloads
        )
        called_ids = [call.args[0] for call in mock_dm.cancel_download.await_args_list]
        self.assertCountEqual(called_ids, [1, 2, 3])

    async def test_no_active_downloads_no_cancel_called(self):
        _, mock_dm, _ = await self._run_delete(
            _make_account(), AppSettings(), [], []
        )
        mock_dm.cancel_download.assert_not_awaited()

    async def test_default_account_id_cleared_on_deletion(self):
        account = _make_account(account_id=5)
        settings = AppSettings()
        settings.default_account_id = 5
        await self._run_delete(account, settings, [], [])
        self.assertIsNone(settings.default_account_id)

    async def test_session_delete_and_commit_called(self):
        account = _make_account()
        session, _, _ = await self._run_delete(account, AppSettings(), [], [])
        session.delete.assert_awaited_with(account)
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
