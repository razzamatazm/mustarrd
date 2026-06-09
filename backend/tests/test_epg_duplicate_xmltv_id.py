"""
Regression test: two channels sharing an epg_channel_id silently zeros one channel's EPG.

Bug: _build_channel_maps stored stream_by_xmltv_id as a plain dict
(xmltv_id -> stream_id), so when two provider channels declared the same
epg_channel_id (common for HD/SD or regional duplicates of one feed) the
last channel in the list silently overwrote the first. Every XMLTV
programme for that id was then attributed only to the winning stream; the
losing channel ended up with zero EPG entries — no log, no error. With a
force refresh (which wipes all rows first) the losing channel's guide went
completely blank.

Fix: stream_by_xmltv_id values are now lists of stream ids. _iter_programs
yields one programme row per mapped stream (epg_id embeds the stream id so
rows stay unique), and _build_channel_maps logs a warning when a duplicate
id is detected.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


def _make_channel(stream_id, name, epg_channel_id, archive_days=7):
    return {
        "stream_id": stream_id,
        "name": name,
        "epg_channel_id": epg_channel_id,
        "tv_archive": 1,
        "tv_archive_duration": archive_days,
    }


_XMLTV = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<channel id="cnn.us"><display-name>CNN</display-name></channel>'
    b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="cnn.us">'
    b"<title>Newsroom</title>"
    b"</programme>"
    b"</tv>"
)


class DuplicateXmltvIdTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

    def _run(self, channels):
        maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(_XMLTV, maps, self.now))

    def test_both_channels_receive_programmes(self):
        """Two streams sharing one epg_channel_id must both get the programme.

        Before the fix, only the last channel in the provider list received
        any XMLTV data; the other silently ended up with zero EPG entries.
        """
        channels = [
            _make_channel("101", "CNN HD", "cnn.us"),
            _make_channel("102", "CNN SD", "cnn.us"),
        ]
        programs = self._run(channels)

        channel_ids = sorted(p["channel_id"] for p in programs)
        self.assertEqual(
            channel_ids,
            ["101", "102"],
            f"Both streams sharing epg_channel_id 'cnn.us' must receive the "
            f"programme. Got channel_ids: {channel_ids}. A last-write-wins map "
            f"silently zeroes one channel's guide.",
        )

    def test_duplicated_rows_have_distinct_epg_ids(self):
        """Rows fanned out to multiple streams must not collide on epg_id."""
        channels = [
            _make_channel("101", "CNN HD", "cnn.us"),
            _make_channel("102", "CNN SD", "cnn.us"),
        ]
        programs = self._run(channels)
        epg_ids = {p["epg_id"] for p in programs}
        self.assertEqual(len(epg_ids), len(programs), "epg_ids must be unique per stream")
        for program in programs:
            self.assertTrue(program["epg_id"].startswith(f"{program['channel_id']}:"))

    def test_duplicate_id_logs_warning(self):
        """Duplicate epg_channel_id must be reported loudly, not swallowed."""
        channels = [
            _make_channel("101", "CNN HD", "cnn.us"),
            _make_channel("102", "CNN SD", "cnn.us"),
        ]
        with self.assertLogs("services.epg_ingest_manager", level="WARNING") as captured:
            self.manager._build_channel_maps(channels)
        self.assertTrue(
            any("cnn.us" in message for message in captured.output),
            f"Expected a warning naming the duplicated id, got: {captured.output}",
        )

    def test_single_channel_unaffected(self):
        """Regression guard: the common single-owner case still yields one row."""
        channels = [_make_channel("101", "CNN HD", "cnn.us")]
        programs = self._run(channels)
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["channel_id"], "101")
        self.assertEqual(programs[0]["channel_name"], "CNN HD")

    def test_per_stream_archive_cutoff_still_applies(self):
        """A stream whose archive window excludes the programme is skipped
        while the other stream still receives it."""
        channels = [
            # 1-day archive: programme ended ~24h ago at the edge; use 30 days vs 0 days
            _make_channel("101", "CNN HD", "cnn.us", archive_days=30),
            _make_channel("102", "CNN SD", "cnn.us", archive_days=1),
        ]
        # Programme ended 2026-06-08 13:00; now is 2026-06-10 12:00 -> outside 1 day, inside 30.
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        maps = self.manager._build_channel_maps(channels)
        programs = list(self.manager._iter_programs(_XMLTV, maps, now))
        channel_ids = [p["channel_id"] for p in programs]
        self.assertEqual(channel_ids, ["101"])


if __name__ == "__main__":
    unittest.main()
