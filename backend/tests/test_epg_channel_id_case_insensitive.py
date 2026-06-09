"""
Regression test: epg_channel_id case mismatch silently drops all programmes.

Provider stream has  epg_channel_id = "BBC.ONE" (uppercase).
XMLTV feed has       <channel id="bbc.one"> (lowercase).
The display-name in the XMLTV does NOT match the provider channel name, so the
name-based fallback also fails.  _iter_programs must map the programme to the
correct stream despite the case difference.

Currently FAILS because stream_by_xmltv_id stores "BBC.ONE" but xmltv_to_stream
lookup uses the raw XMLTV value "bbc.one", a case-sensitive miss.
"""
import sys
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


_XMLTV_TEMPLATE = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <tv>
      <channel id="{channel_id}">
        <display-name>{display_name}</display-name>
      </channel>
      <programme channel="{channel_id}" start="20240101120000 +0000" stop="20240101130000 +0000">
        <title>News at Noon</title>
      </programme>
    </tv>
""")


class EpgChannelIdCaseInsensitiveTests(unittest.TestCase):
    def setUp(self):
        self._mgr = EPGIngestManager()
        self._now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _build_maps(self, epg_channel_id, provider_name):
        channel = {
            "stream_id": "12345",
            "name": provider_name,
            "epg_channel_id": epg_channel_id,
            "tv_archive": "1",
            "tv_archive_duration": "7",
        }
        return self._mgr._build_channel_maps([channel])

    def _programmes(self, xmltv_bytes, channel_maps):
        return list(self._mgr._iter_programs(xmltv_bytes, channel_maps, self._now))

    def test_exact_case_match_yields_programme(self):
        """Sanity check: identical case always works."""
        maps = self._build_maps("bbc.one", "BBC One")
        xmltv = _XMLTV_TEMPLATE.format(
            channel_id="bbc.one", display_name="BBC One"
        ).encode()
        progs = self._programmes(xmltv, maps)
        self.assertEqual(len(progs), 1)
        self.assertEqual(progs[0]["channel_id"], "12345")

    def test_uppercase_provider_id_lowercase_xmltv_id(self):
        """Provider epg_channel_id uppercase; XMLTV channel id lowercase.
        Display-name differs from provider name so name fallback also misses.
        Programmes must still be returned."""
        maps = self._build_maps("BBC.ONE", "BBC One HD")
        # XMLTV uses lowercase id; display-name "BBC One" != provider name "BBC One HD"
        xmltv = _XMLTV_TEMPLATE.format(
            channel_id="bbc.one", display_name="BBC One"
        ).encode()
        progs = self._programmes(xmltv, maps)
        self.assertEqual(
            len(progs),
            1,
            "Programme silently dropped due to case mismatch in epg_channel_id vs XMLTV channel id",
        )
        self.assertEqual(progs[0]["channel_id"], "12345")

    def test_lowercase_provider_id_uppercase_xmltv_id(self):
        """Provider epg_channel_id lowercase; XMLTV channel id uppercase.
        Display-name differs from provider name so name fallback also misses."""
        maps = self._build_maps("bbc.one", "BBC One HD")
        xmltv = _XMLTV_TEMPLATE.format(
            channel_id="BBC.ONE", display_name="BBC One"
        ).encode()
        progs = self._programmes(xmltv, maps)
        self.assertEqual(
            len(progs),
            1,
            "Programme silently dropped due to case mismatch in epg_channel_id vs XMLTV channel id",
        )
        self.assertEqual(progs[0]["channel_id"], "12345")

    def test_mixed_case_provider_id_vs_xmltv_id(self):
        """Provider uses Title-Case; XMLTV uses kebab-lower."""
        maps = self._build_maps("BBC.One.UK", "BBC One UK")
        xmltv = _XMLTV_TEMPLATE.format(
            channel_id="bbc.one.uk", display_name="BBC 1 UK"
        ).encode()
        progs = self._programmes(xmltv, maps)
        self.assertEqual(
            len(progs),
            1,
            "Programme silently dropped due to case mismatch in epg_channel_id vs XMLTV channel id",
        )


if __name__ == "__main__":
    unittest.main()
