"""
Regression test: _process_epg_entry ignores channel-level tv_archive when
setting has_archive on individual EPG programs from the live API.

Many IPTV providers set tv_archive=1 at the channel level but do not include
a has_archive field in individual EPG program listings. The EPG backfill path
in epg_ingest_manager.py correctly handles this with a channel-level fallback:

    has_archive = self._bool_from_value(
        entry.get("has_archive"), fallback=channel_has_archive
    )

The live API fallback path in _process_epg_entry does not:

    "has_archive": int(entry.get("has_archive", 0) or 0) == 1,

Missing field defaults to 0, which becomes False. get_past_programs filters on
has_archive (epg_service.py line 278):

    if program.get("has_archive", False):
        past_programs.append(program)

Result: the Catchup page is silently empty for any provider that:
  1. Sets tv_archive=1 at the channel level, but
  2. Does not include has_archive in individual EPG program entries, and
  3. Is served via the live EPG API path (no XMLTV ingest, empty DB, or fresh=True).

Fix: _process_epg_entry should accept a has_archive_fallback parameter so
callers that know the channel supports archive can supply True.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import EPGService


class LiveEPGHasArchiveFallbackTests(unittest.TestCase):
    def setUp(self):
        self.svc = EPGService()

    def _entry(self, *, has_archive=None):
        e = {
            "title": "Evening News",
            "start_timestamp": 1748995200,
            "stop_timestamp": 1748998800,
            "channel_id": "42",
        }
        if has_archive is not None:
            e["has_archive"] = has_archive
        return e

    def test_missing_has_archive_defaults_false(self):
        """
        BUG: When the provider omits has_archive from EPG entries, _process_epg_entry
        returns has_archive=False. For a channel with tv_archive=1 this is wrong:
        missing should fall back to True so get_past_programs does not silently
        discard every program on the Catchup page.

        This assertion documents the broken current behavior and is expected to FAIL
        until _process_epg_entry accepts a has_archive_fallback parameter and
        callers on archive channels pass has_archive_fallback=True.
        """
        result = self.svc._process_epg_entry(self._entry())
        self.assertTrue(
            result["has_archive"],
            "has_archive should default True for archive channels when the field is absent; "
            "currently defaults False, causing silent empty Catchup pages",
        )

    def test_explicit_zero_stays_false(self):
        """Explicit has_archive=0 must still produce False (provider said no)."""
        result = self.svc._process_epg_entry(self._entry(has_archive=0))
        self.assertFalse(result["has_archive"])

    def test_explicit_one_stays_true(self):
        """Explicit has_archive=1 must produce True."""
        result = self.svc._process_epg_entry(self._entry(has_archive=1))
        self.assertTrue(result["has_archive"])

    def test_explicit_string_zero_stays_false(self):
        """Providers sometimes return string values; '0' must produce False."""
        result = self.svc._process_epg_entry(self._entry(has_archive="0"))
        self.assertFalse(result["has_archive"])

    def test_explicit_string_one_stays_true(self):
        """Providers sometimes return string values; '1' must produce True."""
        result = self.svc._process_epg_entry(self._entry(has_archive="1"))
        self.assertTrue(result["has_archive"])


if __name__ == "__main__":
    unittest.main()
