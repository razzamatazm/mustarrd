"""
Regression test: POST /epg/refresh TOCTOU race allows two simultaneous refreshes.

Before the fix:
- `status["running"]` is only set True INSIDE `_refresh_all_accounts()`, which runs
  after the asyncio task is scheduled and after the lock is acquired.
- Two rapid POST /epg/refresh requests both read running=False, both call
  asyncio.create_task(), both return HTTP 200.
- Task B waits for the lock, then runs a full second EPG refresh after Task A finishes.
- With force=True this wipes and reloads the EPG table twice.

After the fix:
- `try_claim_refresh()` atomically sets `_task_pending=True` and returns True on the
  first call.  A second call before the task starts returns False immediately.
- The API endpoint uses `try_claim_refresh()` so the second request gets 409.
- `get_status()` reflects `_task_pending` as running=True so the UI also shows busy.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class EPGRefreshRaceTests(unittest.TestCase):

    def setUp(self):
        self.manager = EPGIngestManager()

    def test_try_claim_refresh_succeeds_when_idle(self):
        """try_claim_refresh must return True when no refresh is running or pending."""
        self.assertTrue(self.manager.try_claim_refresh())

    def test_try_claim_refresh_fails_when_pending(self):
        """Second try_claim_refresh call must return False while first is still pending."""
        self.manager.try_claim_refresh()
        self.assertFalse(
            self.manager.try_claim_refresh(),
            "Second claim must fail: a refresh is already pending.",
        )

    def test_get_status_reports_running_when_pending(self):
        """get_status must report running=True while a task is pending but not yet started."""
        self.manager.try_claim_refresh()
        status = self.manager.get_status()
        self.assertTrue(
            status.get("running"),
            "get_status must return running=True when _task_pending is set.",
        )

    def test_try_claim_refresh_fails_when_status_running(self):
        """try_claim_refresh must return False when _status['running'] is already True."""
        self.manager._status["running"] = True
        self.assertFalse(
            self.manager.try_claim_refresh(),
            "Claim must fail when a refresh is already running.",
        )

    def test_task_pending_cleared_after_refresh_all_accounts_called(self):
        """_task_pending must be False once refresh_all_accounts() starts executing."""
        import asyncio

        self.manager.try_claim_refresh()
        self.assertTrue(self.manager._task_pending)

        cleared = []

        async def run():
            original = self.manager._refresh_all_accounts

            async def spy(*args, **kwargs):
                cleared.append(self.manager._task_pending)

            self.manager._refresh_all_accounts = spy
            try:
                await self.manager.refresh_all_accounts()
            finally:
                self.manager._refresh_all_accounts = original

        asyncio.run(run())

        self.assertEqual(
            cleared,
            [False],
            "_task_pending must be False by the time refresh_all_accounts body executes.",
        )


if __name__ == "__main__":
    unittest.main()
