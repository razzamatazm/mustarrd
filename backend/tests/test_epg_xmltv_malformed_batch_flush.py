"""
Regression tests for malformed XMLTV mid-parse discarding accumulated EPG batch.

When a provider's XMLTV file is truncated or contains invalid XML, ET.iterparse
raises ParseError inside _iter_programs. Before the fix the exception propagated
out of the generator, bypassing the caller's flush_batch call. Any programmes
yielded before the error were never written to the database.

After the fix _iter_programs catches ParseError, logs a warning, and stops
iteration cleanly. The caller's flush sees the accumulated batch and saves it.

Observable contract tested here: list(_iter_programs(malformed_xmltv)) returns
all programmes parsed before the malformed element and does NOT raise ParseError.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from defusedxml import ElementTree as ET
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


XMLTV_TRUNCATED = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv>'
    b'<channel id="bbc1"><display-name>BBC One</display-name></channel>'
    b'<programme start="20240101120000 +0000" stop="20240101130000 +0000" channel="bbc1">'
    b'<title>News at Noon</title></programme>'
    b'<programme start="20240101130000 +0000" stop="20240101140000 +0000" channel="bbc1">'
    b'<title>Afternoon Show</title></programme>'
    # Truncated: unclosed start tag, no </tv>
    b'<programme start="20240101140000 +0000"'
)

XMLTV_EMBEDDED_NUL = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv>'
    b'<channel id="ch1"><display-name>CNN</display-name></channel>'
    b'<programme start="20240101100000 +0000" stop="20240101110000 +0000" channel="ch1">'
    b'<title>World News</title></programme>'
    # NUL byte is invalid in XML 1.0
    b'\x00'
    b'<programme start="20240101110000 +0000" stop="20240101120000 +0000" channel="ch1">'
    b'<title>This Should Not Appear</title></programme>'
    b'</tv>'
)

XMLTV_WELL_FORMED = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv>'
    b'<channel id="bbc1"><display-name>BBC One</display-name></channel>'
    b'<programme start="20240101120000 +0000" stop="20240101130000 +0000" channel="bbc1">'
    b'<title>News at Noon</title></programme>'
    b'<programme start="20240101130000 +0000" stop="20240101140000 +0000" channel="bbc1">'
    b'<title>Afternoon Show</title></programme>'
    b'</tv>'
)


class MalformedXmltvBatchFlushTests(unittest.TestCase):
    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _collect(self, channels, xmltv_bytes):
        channel_maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(xmltv_bytes, channel_maps, self.now))

    def test_truncated_xmltv_does_not_raise(self):
        """_iter_programs must not propagate ParseError on truncated input.

        Before the fix ET.iterparse raised ParseError which bypassed the
        caller's flush_batch, silently discarding all programmes parsed so far.
        """
        channels = [_make_channel("101", "BBC One", epg_channel_id="bbc1")]
        try:
            programs = self._collect(channels, XMLTV_TRUNCATED)
        except ET.ParseError:
            self.fail(
                "_iter_programs raised ParseError on truncated XMLTV. "
                "Programmes parsed before the truncation point are lost. "
                "Fix: wrap ET.iterparse loop in try/except ET.ParseError and return."
            )
        self.assertEqual(
            len(programs),
            2,
            f"Expected 2 programmes before truncation, got {len(programs)}.",
        )

    def test_embedded_nul_does_not_raise(self):
        """_iter_programs must not raise on XML containing an invalid NUL byte.

        Only the programme before the NUL should be returned.
        """
        channels = [_make_channel("101", "CNN", epg_channel_id="ch1")]
        try:
            programs = self._collect(channels, XMLTV_EMBEDDED_NUL)
        except ET.ParseError:
            self.fail(
                "_iter_programs raised ParseError on XMLTV with embedded NUL. "
                "Fix: catch ParseError and stop iteration cleanly."
            )
        titles = [p["title"] for p in programs]
        self.assertIn("World News", titles)
        self.assertNotIn(
            "This Should Not Appear",
            titles,
            "Programme after the malformed byte must not be yielded.",
        )

    def test_well_formed_xmltv_still_works(self):
        """Regression: well-formed XMLTV must yield all programmes unchanged."""
        channels = [_make_channel("101", "BBC One", epg_channel_id="bbc1")]
        programs = self._collect(channels, XMLTV_WELL_FORMED)
        self.assertEqual(
            len(programs),
            2,
            f"Expected 2 programmes from well-formed XMLTV, got {len(programs)}.",
        )
        titles = {p["title"] for p in programs}
        self.assertIn("News at Noon", titles)
        self.assertIn("Afternoon Show", titles)


if __name__ == "__main__":
    unittest.main()
