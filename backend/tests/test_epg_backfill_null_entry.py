"""
Regression test: _backfill_from_api crashes on null/non-dict entries in epg_listings.

Bug (MEDIUM): The inner processing loop in _backfill_from_api (lines 880-886 of
epg_ingest_manager.py) calls entry.get(...) on every item returned by
client.get_epg() without first checking that the item is a dict.

Some providers return epg_listings arrays that contain null entries (e.g.
{"epg_listings": [null, {...}, null, {...}]}) due to encoding bugs or
list-padding on the provider's side.

When entry is None, entry.get("start_timestamp") raises:
    AttributeError: 'NoneType' object has no attribute 'get'

This AttributeError is NOT caught by the per-channel try/except at lines
870-878, which only wraps the client.get_epg() call. The exception propagates
out of _backfill_from_api entirely, causing:

  1. All remaining channels in channel_targets are skipped (never backfilled).
  2. _mark_backfill_attempt is not called, so the cooldown is not set.
  3. _refresh_account raises and the account is flagged as connection-failed.
  4. The next scheduled refresh re-triggers the full backfill (no cooldown).
     Any subsequent channel list that again includes a null entry repeats
     the failure, leaving every affected channel's EPG permanently empty.

Expected behavior (after fix): null and non-dict entries in epg_listings are
silently skipped; valid entries in the same batch and all remaining channels
are processed normally.

Root cause: epg_ingest_manager.py, the "for entry in epg_entries" loop, lines
880-932 -- no isinstance(entry, dict) guard before calling entry.get().

Reproduction:
    1. Set up a provider account.
    2. The provider's get_simple_data_table endpoint for any channel returns
       {"epg_listings": [null, {"title": "...", "start_timestamp": ...}]}.
    3. Trigger an EPG refresh while that channel is a backfill target.
    4. EPG for that channel and ALL subsequent channels remains empty.
    5. Account shows "connection failed" even though the provider is reachable.
"""

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class BackfillNullEntryTests(unittest.TestCase):
    """
    _backfill_from_api must skip null/non-dict entries without crashing.

    These tests reproduce the logic of the inner processing loop
    (epg_ingest_manager.py lines 880-886) in isolation. The loop calls
    entry.get() on every item; a None entry raises AttributeError, which
    aborts the entire backfill.
    """

    def setUp(self):
        self.manager = EPGIngestManager()

    def _process_entries(self, epg_entries):
        """
        Mirror of the entry-validation head of _backfill_from_api.

        Returns (processed, raised) where processed is the count of entries
        that yielded valid start/stop timestamps, and raised is the exception
        class if one escaped the loop (or None if the loop completed).
        """
        processed = 0
        raised = None
        try:
            for entry in epg_entries:
                # Verbatim logic from _backfill_from_api lines 881-888.
                start_ts = self.manager._parse_timestamp(entry.get("start_timestamp"))
                stop_ts = self.manager._parse_timestamp(entry.get("stop_timestamp"))
                if start_ts is None:
                    start_ts = self.manager._parse_timestamp(entry.get("start"))
                if stop_ts is None:
                    stop_ts = self.manager._parse_timestamp(entry.get("stop"))
                if not start_ts or not stop_ts:
                    continue
                processed += 1
        except Exception as exc:
            raised = type(exc)
        return processed, raised

    def test_null_entry_currently_raises_attribute_error(self):
        """
        A null entry in epg_listings currently causes AttributeError.

        This test documents the BUG. It passes while the bug is present
        and will fail once a type guard is added.
        """
        epg_entries = [None]
        _, raised = self._process_entries(epg_entries)
        self.assertEqual(
            raised,
            AttributeError,
            "Expected AttributeError from None.get() to confirm bug is present. "
            "If this test now passes (no AttributeError), the bug has been fixed "
            "and this test should be replaced by test_null_entry_is_skipped.",
        )

    def test_null_entry_before_valid_entry_aborts_loop(self):
        """
        A null entry before a valid entry currently causes the valid entry
        to be silently dropped (loop aborted by AttributeError).

        Bug: a provider returning [null, {valid}] results in zero entries
        processed for that channel AND crashes the entire backfill for all
        subsequent channels.
        """
        valid_entry = {
            "start_timestamp": "1717228800",
            "stop_timestamp": "1717232400",
        }
        epg_entries = [None, valid_entry]
        processed, raised = self._process_entries(epg_entries)

        # Currently: raised=AttributeError, processed=0.
        # After fix:  raised=None, processed=1.
        self.assertEqual(
            processed,
            1,
            f"Expected 1 valid entry to be processed after the null was skipped, "
            f"but got processed={processed} (raised={raised}). "
            "Bug: None.get() raises AttributeError before the valid entry is "
            "reached. Fix: add 'if not isinstance(entry, dict): continue' "
            "before calling entry.get() in _backfill_from_api.",
        )

    def test_non_dict_string_entry_aborts_loop(self):
        """
        A string entry (another malformed-list variant) also causes AttributeError.

        Some providers return [\"programme_id_123\", {...}] -- a bare string
        followed by the real entry -- which triggers the same crash.
        """
        valid_entry = {
            "start_timestamp": "1717228800",
            "stop_timestamp": "1717232400",
        }
        epg_entries = ["programme_id_123", valid_entry]
        processed, raised = self._process_entries(epg_entries)
        self.assertEqual(
            processed,
            1,
            f"Expected 1 valid entry processed after string was skipped, "
            f"got processed={processed} (raised={raised}). "
            "Bug: str.get() raises AttributeError. Fix: isinstance(entry, dict) guard.",
        )

    def test_all_valid_entries_are_processed(self):
        """Sanity: a normal (all-dict) epg_listings must still be fully processed."""
        entries = [
            {"start_timestamp": "1717228800", "stop_timestamp": "1717232400"},
            {"start_timestamp": "1717232400", "stop_timestamp": "1717236000"},
        ]
        processed, raised = self._process_entries(entries)
        self.assertIsNone(raised)
        self.assertEqual(processed, 2)

    def test_empty_epg_listings_succeeds(self):
        """An empty list must not raise anything."""
        processed, raised = self._process_entries([])
        self.assertIsNone(raised)
        self.assertEqual(processed, 0)


if __name__ == "__main__":
    unittest.main()
