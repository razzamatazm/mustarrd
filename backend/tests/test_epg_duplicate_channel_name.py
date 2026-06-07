"""
Regression test for duplicate normalized channel names in EPG ingest.

When two catchup channels share the same normalized name and neither has
epg_channel_id set, _build_channel_maps overwrites the first channel in
stream_by_name.  XMLTV programs matched by display-name then route to the
LAST channel added, and the first channel receives no guide data.

Expected: adding a second channel with the same name does not silently
deprive the first channel of its XMLTV-matched programs.
"""

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from datetime import datetime, timezone
from services.epg_ingest_manager import EPGIngestManager


def _make_channel(stream_id: str, name: str) -> dict:
    return {
        "stream_id": stream_id,
        "name": name,
        "tv_archive": 1,
        "tv_archive_duration": 7,
        # no epg_channel_id: these channels rely on name-based XMLTV matching
    }


def _make_xmltv(xmltv_channel_id: str, display_name: str) -> bytes:
    """Minimal XMLTV with one channel and one programme."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<tv>'
        b'<channel id="' + xmltv_channel_id.encode() + b'">'
        b'<display-name>' + display_name.encode() + b'</display-name>'
        b'</channel>'
        b'<programme channel="' + xmltv_channel_id.encode() + b'" '
        b'start="20240101120000 +0000" stop="20240101130000 +0000">'
        b'<title>News Hour</title>'
        b'</programme>'
        b'</tv>'
    )


class DuplicateChannelNameTests(unittest.TestCase):
    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _collect_programs(self, channels: list, xmltv_bytes: bytes) -> list:
        channel_maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(xmltv_bytes, channel_maps, self.now))

    # ------------------------------------------------------------------
    # Baseline: single channel with matching name routes programs correctly.
    # ------------------------------------------------------------------

    def test_single_channel_name_match_routes_to_correct_stream(self):
        channels = [_make_channel("101", "CNN")]
        xmltv = _make_xmltv("cnn-us", "CNN")
        programs = self._collect_programs(channels, xmltv)
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["channel_id"], "101")

    # ------------------------------------------------------------------
    # Bug: second channel with same normalized name overwrites the first
    # in stream_by_name.  Programs go to stream 102, stream 101 gets none.
    #
    # This test FAILS on current code and must PASS after the fix.
    # ------------------------------------------------------------------

    @unittest.expectedFailure
    def test_first_channel_retains_epg_when_second_has_same_name(self):
        """Adding a second channel with the same normalized name must not
        silently deprive the first channel of its XMLTV-matched programs."""
        channels = [
            _make_channel("101", "CNN"),   # comes first in the provider list
            _make_channel("102", "cnn"),   # same normalized name, different stream
        ]
        xmltv = _make_xmltv("cnn-us", "CNN")
        programs = self._collect_programs(channels, xmltv)

        stream_ids_with_data = {p["channel_id"] for p in programs}
        # Stream 101 (the first channel with this name) must receive programs.
        # Before the fix: stream_ids_with_data == {"102"} and this assertion fails.
        self.assertIn(
            "101",
            stream_ids_with_data,
            "Stream 101 (first channel named 'CNN') received no programs; "
            "they were silently routed to stream 102 because duplicate channel "
            "names are not handled in _build_channel_maps.",
        )

    @unittest.expectedFailure
    def test_duplicate_name_second_channel_does_not_steal_all_programs(self):
        """When two channels share a name, all programs must not go to only
        one of them via the name-based fallback."""
        channels = [
            _make_channel("101", "BBC One"),
            _make_channel("102", "bbc one"),  # identical after normalization
        ]
        xmltv = _make_xmltv("bbc1", "BBC One")
        programs = self._collect_programs(channels, xmltv)

        # With two same-named channels and one XMLTV entry, at least one
        # channel must get the programs.  The critical failure is that the
        # first channel gets ZERO programs while the second gets all of them.
        self.assertTrue(
            len(programs) > 0,
            "No programs were yielded at all for a valid XMLTV entry.",
        )
        # Stream 101 should not be completely excluded by stream 102.
        stream_ids_with_data = {p["channel_id"] for p in programs}
        self.assertIn(
            "101",
            stream_ids_with_data,
            "All programs went to stream 102; stream 101 (first matching "
            "channel) got none due to the last-write-wins dict collision.",
        )


if __name__ == "__main__":
    unittest.main()
