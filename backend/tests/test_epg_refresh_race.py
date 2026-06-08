"""
Regression test: POST /epg/refresh TOCTOU race allows two simultaneous refreshes.

Before the fix:
- `_task_pending` was cleared at the top of `refresh_all_accounts()`, before the first
  await (`self._log(...)`). Between that clear and `_status["running"]` being set True
  inside `_refresh_all_accounts()` (after the DB session await), both flags were False.
- A second POST /epg/refresh arriving during that window would see both flags False,
  call `try_claim_refresh()` successfully, and create a second task. With force=True
  this wiped and reloaded the EPG table twice.

After the fix:
- `_task_pending` is cleared only after `_status["running"]` is set True inside
  `_refresh_all_accounts()`. Both assignments are synchronous with no await between
  them, so there is no window where both flags are False.
- `try_claim_refresh()` blocks on `_task_pending` until `running` takes over, so the
  second request always gets 409.
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

    def test_second_claim_blocked_during_log_await(self):
        """try_claim_refresh must return False even while the first task is suspended
        at an await point inside refresh_all_accounts(), before running is set True.

        This is the interleaved path Cleo identified: _task_pending was cleared at the
        top of refresh_all_accounts before any await, leaving a window where both
        _task_pending and running were False. The fix keeps _task_pending True until
        running is set True inside _refresh_all_accounts.
        """
        import asyncio

        manager = EPGIngestManager()
        second_claim_result = []
        log_was_awaited = asyncio.Event()
        log_may_proceed = asyncio.Event()

        async def pausing_log(*args, **kwargs):
            log_was_awaited.set()
            await log_may_proceed.wait()

        async def fake_refresh_all(*args, **kwargs):
            manager._status.update({"running": True})
            manager._task_pending = False

        manager._log = pausing_log
        manager._refresh_all_accounts = fake_refresh_all

        async def run():
            manager.try_claim_refresh()
            task = asyncio.create_task(manager.refresh_all_accounts())
            await log_was_awaited.wait()
            second_claim_result.append(manager.try_claim_refresh())
            log_may_proceed.set()
            await task

        asyncio.run(run())

        self.assertFalse(
            second_claim_result[0],
            "Second claim must be blocked even while refresh task is suspended at _log await.",
        )


    def test_release_pending_on_task_failure_unblocks_next_claim(self):
        """If the refresh task fails before running is set True, release_pending
        (called via the done-callback) must clear _task_pending so the next
        try_claim_refresh call succeeds rather than returning 409 forever.
        """
        import asyncio

        manager = EPGIngestManager()

        async def failing_refresh(*args, **kwargs):
            raise RuntimeError("SQLite locked")

        manager._refresh_all_accounts = failing_refresh

        async def run():
            manager.try_claim_refresh()
            task = asyncio.create_task(manager.refresh_all_accounts())
            manager._pending_task = task
            task.add_done_callback(manager.release_pending)
            try:
                await task
            except RuntimeError:
                pass

        asyncio.run(run())

        self.assertFalse(
            manager._task_pending,
            "release_pending must clear _task_pending after task failure.",
        )
        self.assertTrue(
            manager.try_claim_refresh(),
            "try_claim_refresh must succeed after release_pending clears the flag "
            "(refresh button must not be permanently bricked by a transient error).",
        )


    def test_stale_done_callback_does_not_clear_later_pending_claim(self):
        """A completed Task A's release_pending callback must not clear the pending
        slot claimed by Task B, which was created after A finished.

        Timeline reproduced here:
        1. Task A finishes normally; _task_pending is already False.
           _pending_task still points at A.
        2. Before A's done-callback runs, a new claim succeeds and Task B is created.
           _pending_task now points at B, _task_pending is True.
        3. A's deferred release_pending(A) fires. Without the ownership guard it
           would see _task_pending=True and clear it, stranding B's pending slot.
        4. A third request slips through try_claim_refresh() and a duplicate refresh starts.

        We use MagicMock sentinels as task stand-ins: release_pending only checks
        object identity, not asyncio internals, so real Task objects are not needed.
        """
        from unittest.mock import MagicMock

        manager = EPGIngestManager()
        task_a = MagicMock(name="task_a")
        task_b = MagicMock(name="task_b")

        # Task A finished; _pending_task still points at A.
        manager._pending_task = task_a
        manager._task_pending = False

        # Task B is now claimed and registered.
        manager._task_pending = True
        manager._pending_task = task_b

        # A's late done-callback fires.
        manager.release_pending(task_a)

        self.assertTrue(
            manager._task_pending,
            "release_pending from an earlier task must not clear a newer task's pending flag.",
        )


if __name__ == "__main__":
    unittest.main()
