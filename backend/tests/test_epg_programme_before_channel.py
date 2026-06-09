"""
Regression test: XMLTV programmes appearing before their <channel> element are dropped.

Bug: _iter_programs built its xmltv-id -> stream-id mapping incrementally
during a single pass over the document. A <programme> element that appeared
before its <channel> element could only be matched if the id was already
known from the provider's channel API (epg_channel_id). For channels matched
by display-name, or matched because the XMLTV id equals the stream id, the
mapping was created only when the <channel> element was reached — so every
programme placed before it in the document was silently dropped.

The XMLTV format does not guarantee that channel definitions precede
programmes, and some providers interleave or append them.

Fix: a pre-scan pass (_scan_channel_map) builds the complete mapping from
all <channel> elements before programmes are processed, so element order no
longer matters.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


# Programme appears BEFORE the channel element that maps "cnn.us" -> CNN.
_XMLTV_PROGRAMME_FIRST = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="cnn.us">'
    b"<title>Newsroom</title>"
    b"</programme>"
    b'<channel id="cnn.us"><display-name>CNN</display-name></channel>'
    b"</tv>"
)

# Same document with the conventional channel-first ordering.
_XMLTV_CHANNEL_FIRST = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<channel id="cnn.us"><display-name>CNN</display-name></channel>'
    b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="cnn.us">'
    b"<title>Newsroom</title>"
    b"</programme>"
    b"</tv>"
)

# Programme whose channel id equals the provider stream id, channel element after.
_XMLTV_STREAM_ID_PROGRAMME_FIRST = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<programme start="20260608120000 +0000" stop="20260608130000 +0000" channel="101">'
    b"<title>Newsroom</title>"
    b"</programme>"
    b'<channel id="101"><display-name>Some Other Name</display-name></channel>'
    b"</tv>"
)


class ProgrammeBeforeChannelTests(unittest.TestCase):

    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

    def _maps_name_matched(self):
        # Channel has no epg_channel_id: it is reachable only via the
        # display-name match, which requires the <channel> element.
        channels = [
            {"stream_id": "101", "name": "CNN", "tv_archive": 1, "tv_archive_duration": 7},
        ]
        return self.manager._build_channel_maps(channels)

    def test_programme_before_channel_is_imported(self):
        """A programme placed before its <channel> element must still be matched."""
        programs = list(self.manager._iter_programs(
            _XMLTV_PROGRAMME_FIRST, self._maps_name_matched(), self.now
        ))
        self.assertEqual(
            len(programs),
            1,
            "Programme appearing before its <channel> element was dropped. "
            "XMLTV does not guarantee element order; the channel map must be "
            "built before programmes are matched.",
        )
        self.assertEqual(programs[0]["channel_id"], "101")
        self.assertEqual(programs[0]["title"], "Newsroom")

    def test_programme_before_channel_matches_channel_first_result(self):
        """Both element orders must produce identical programme rows."""
        before = list(self.manager._iter_programs(
            _XMLTV_PROGRAMME_FIRST, self._maps_name_matched(), self.now
        ))
        after = list(self.manager._iter_programs(
            _XMLTV_CHANNEL_FIRST, self._maps_name_matched(), self.now
        ))
        self.assertEqual(before, after)

    def test_programme_before_channel_stream_id_identity(self):
        """Programme matched via xmltv-id == stream-id must survive reordering."""
        programs = list(self.manager._iter_programs(
            _XMLTV_STREAM_ID_PROGRAMME_FIRST, self._maps_name_matched(), self.now
        ))
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["channel_id"], "101")


if __name__ == "__main__":
    unittest.main()
