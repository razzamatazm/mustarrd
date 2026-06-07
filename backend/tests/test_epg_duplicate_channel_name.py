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


def _make_channel(stream_id: str, name: str, epg_channel_id: str = "") -> dict:
    ch = {
        "stream_id": stream_id,
        "name": name,
        "tv_archive": 1,
        "tv_archive_duration": 7,
    }
    if epg_channel_id:
        ch["epg_channel_id"] = epg_channel_id
    return ch


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


def _make_xmltv_two_channels(
    ch1_id: str, ch1_name: str, ch2_id: str, ch2_name: str
) -> bytes:
    """Minimal XMLTV with two channels, each with one programme."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<tv>'
        b'<channel id="' + ch1_id.encode() + b'">'
        b'<display-name>' + ch1_name.encode() + b'</display-name>'
        b'</channel>'
        b'<channel id="' + ch2_id.encode() + b'">'
        b'<display-name>' + ch2_name.encode() + b'</display-name>'
        b'</channel>'
        b'<programme channel="' + ch1_id.encode() + b'" '
        b'start="20240101120000 +0000" stop="20240101130000 +0000">'
        b'<title>Morning News</title>'
        b'</programme>'
        b'<programme channel="' + ch2_id.encode() + b'" '
        b'start="20240101140000 +0000" stop="20240101150000 +0000">'
        b'<title>Afternoon News</title>'
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
    # Regression: second channel with same normalized name must not overwrite
    # the first in stream_by_name (was last-write-wins before the fix).
    # ------------------------------------------------------------------

    def test_first_channel_retains_epg_when_second_has_same_name(self):
        """First channel with a given normalized name retains XMLTV programs
        when a second channel has the same normalized name."""
        channels = [
            _make_channel("101", "CNN"),   # comes first in the provider list
            _make_channel("102", "cnn"),   # same normalized name, different stream
        ]
        xmltv = _make_xmltv("cnn-us", "CNN")
        programs = self._collect_programs(channels, xmltv)

        stream_ids_with_data = {p["channel_id"] for p in programs}
        self.assertIn(
            "101",
            stream_ids_with_data,
            "Stream 101 (first channel named 'CNN') received no programs; "
            "they were silently routed to stream 102 because duplicate channel "
            "names are not handled in _build_channel_maps.",
        )

    def test_duplicate_name_second_channel_does_not_steal_all_programs(self):
        """When two channels share a name, all programs must not go to only
        one of them via the name-based fallback."""
        channels = [
            _make_channel("101", "BBC One"),
            _make_channel("102", "bbc one"),  # identical after normalization
        ]
        xmltv = _make_xmltv("bbc1", "BBC One")
        programs = self._collect_programs(channels, xmltv)

        self.assertTrue(len(programs) > 0, "No programs yielded for a valid XMLTV entry.")
        stream_ids_with_data = {p["channel_id"] for p in programs}
        self.assertIn(
            "101",
            stream_ids_with_data,
            "Stream 101 (first channel named 'BBC One') received no programs.",
        )


class MixedExplicitIdAndNameOnlyTests(unittest.TestCase):
    """Channels where one has an explicit epg_channel_id and a near-duplicate
    has none must both receive their respective XMLTV programs after the fix."""

    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _collect_programs(self, channels: list, xmltv_bytes: bytes) -> list:
        channel_maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(xmltv_bytes, channel_maps, self.now))

    def test_name_only_channel_gets_name_fallback_when_peer_has_explicit_id(self):
        """Channel 102 (name-only) must receive programs from a second XMLTV
        channel whose id is not registered, falling back to the display-name
        slot. Channel 101 (explicit epg_channel_id) must receive programs from
        its own id-matched XMLTV channel. Both must be served."""
        channels = [
            # 101 has an explicit id: it reaches XMLTV via stream_by_xmltv_id.
            _make_channel("101", "CNN", epg_channel_id="cnn-us"),
            # 102 is name-only: it can only reach XMLTV via stream_by_name.
            _make_channel("102", "cnn"),
        ]
        # Two XMLTV channels with the same display-name "CNN":
        #   cnn-us  -> matched to 101 via explicit id
        #   cnn-alt -> unregistered id, must fall back to name -> should reach 102
        xmltv = _make_xmltv_two_channels("cnn-us", "CNN", "cnn-alt", "CNN")
        programs = self._collect_programs(channels, xmltv)

        by_stream = {}
        for p in programs:
            by_stream.setdefault(p["channel_id"], []).append(p)

        self.assertIn(
            "101",
            by_stream,
            "Channel 101 (explicit epg_channel_id='cnn-us') received no programs.",
        )
        self.assertIn(
            "102",
            by_stream,
            "Channel 102 (name-only) received no programs. The name-fallback slot "
            "was stolen by channel 101 (which has an explicit xmltv id and does not "
            "need it), starving the name-only channel of all guide data.",
        )

    def test_channel_order_does_not_affect_name_slot_priority(self):
        """Name-only channels must win the name slot regardless of order in
        the provider list (id-having channel appearing first must not block it)."""
        # id-having channel comes first this time
        channels = [
            _make_channel("101", "CNN", epg_channel_id="cnn-us"),
            _make_channel("102", "cnn"),
        ]
        xmltv = _make_xmltv_two_channels("cnn-us", "CNN", "cnn-alt", "CNN")
        programs_id_first = self._collect_programs(channels, xmltv)

        # name-only channel comes first
        channels_reversed = [
            _make_channel("102", "cnn"),
            _make_channel("101", "CNN", epg_channel_id="cnn-us"),
        ]
        programs_name_first = self._collect_programs(channels_reversed, xmltv)

        def stream_ids(programs):
            return {p["channel_id"] for p in programs}

        self.assertEqual(
            stream_ids(programs_id_first),
            stream_ids(programs_name_first),
            "Program routing changed based on channel order in the provider list. "
            "The name slot must always go to the name-only channel regardless of order.",
        )


if __name__ == "__main__":
    unittest.main()
