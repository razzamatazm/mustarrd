"""
Regression tests for Unicode normalization gaps in _normalize_name.

_normalize_name (epg_ingest_manager.py:694) does only .lower().split(), which:
  - does NOT strip invisible Unicode format chars (U+200B zero-width space,
    U+FEFF BOM, etc.) that some IPTV encoders inject into channel names
  - does NOT apply unicodedata.normalize('NFC', ...), so a provider channel
    named in NFD form ("café HD") silently fails to match an XMLTV
    display-name in NFC form ("é"), leaving the channel with no EPG

In all three cases below the channel maps yield NO programs because the
normalized provider name and normalized XMLTV display-name are different
strings even though they look identical to a human.

Expected (after fix): all three channels get EPG matched.
Actual (before fix): all three fail to match and receive zero programs.
"""

import sys
import unicodedata
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
    }


def _make_xmltv(xmltv_channel_id: str, display_name: str) -> bytes:
    dn_bytes = display_name.encode("utf-8")
    cid_bytes = xmltv_channel_id.encode("utf-8")
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<tv>"
        b'<channel id="' + cid_bytes + b'">'
        b"<display-name>" + dn_bytes + b"</display-name>"
        b"</channel>"
        b'<programme channel="' + cid_bytes + b'" '
        b'start="20240101120000 +0000" stop="20240101130000 +0000">'
        b"<title>News Hour</title>"
        b"</programme>"
        b"</tv>"
    )


class NormalizeNameUnicodeTests(unittest.TestCase):
    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _programs_for(self, provider_name: str, xmltv_display_name: str) -> list:
        channels = [_make_channel("101", provider_name)]
        xmltv = _make_xmltv("ch-101", xmltv_display_name)
        maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(xmltv, maps, self.now))

    def test_zero_width_space_in_provider_name_should_not_block_epg_match(self):
        """Provider sends 'BBC<U+200B>HD' (zero-width space between BBC and HD,
        no real space). XMLTV display-name is 'BBCHD' (no space either). After
        stripping the invisible U+200B, both sides normalize to 'bbchd' and
        the channel should receive EPG.

        Before fix: _normalize_name keeps U+200B, normalized strings differ,
        channel receives zero programs.
        """
        # U+200B = ZERO WIDTH SPACE, injected by some IPTV encoders instead of a real space
        provider_name = "BBC\u200bHD"
        xmltv_name = "BBCHD"

        programs = self._programs_for(provider_name, xmltv_name)
        self.assertEqual(
            len(programs),
            1,
            f"Channel with zero-width space in provider name received no EPG. "
            f"provider normalized='{self.manager._normalize_name(provider_name)}', "
            f"xmltv normalized='{self.manager._normalize_name(xmltv_name)}'.",
        )
        self.assertEqual(programs[0]["channel_id"], "101")

    def test_nfd_provider_name_matches_nfc_xmltv_display_name(self):
        """Provider channel name is in NFD Unicode form ('cafe<combining-acute> HD').
        XMLTV display-name is in NFC form ('<e-acute> HD'). They are the same
        string under Unicode equivalence (NFC normalization) and must match.

        Before fix: _normalize_name does not call unicodedata.normalize, so
        the NFD and NFC byte sequences compare unequal, silently dropping EPG.
        """
        nfc_name = unicodedata.normalize("NFC", "café HD")   # NFC: e-acute as single codepoint
        nfd_name = unicodedata.normalize("NFD", "café HD")   # NFD: e + combining acute

        self.assertNotEqual(
            nfc_name,
            nfd_name,
            "Test setup error: NFC and NFD forms are already equal.",
        )
        programs = self._programs_for(nfd_name, nfc_name)
        self.assertEqual(
            len(programs),
            1,
            f"Channel whose provider name is NFD ('{nfd_name!r}') received no EPG "
            f"when XMLTV display-name is NFC ('{nfc_name!r}'). "
            "Unicode normalization missing from _normalize_name.",
        )
        self.assertEqual(programs[0]["channel_id"], "101")

    def test_bom_in_provider_name_should_not_block_epg_match(self):
        """Provider sends '<U+FEFF>BBC HD' (byte-order mark prepended by some
        IPTV encoders). XMLTV display-name is 'BBC HD'. After stripping the BOM,
        they should normalize identically.

        Before fix: _normalize_name keeps U+FEFF, so '<U+FEFF>bbc hd' != 'bbc hd',
        channel receives zero programs.
        """
        provider_name = "\ufeffBBC HD"
        xmltv_name = "BBC HD"

        programs = self._programs_for(provider_name, xmltv_name)
        self.assertEqual(
            len(programs),
            1,
            f"Channel with BOM in provider name received no EPG. "
            f"provider normalized='{self.manager._normalize_name(provider_name)}', "
            f"xmltv normalized='{self.manager._normalize_name(xmltv_name)}'.",
        )
        self.assertEqual(programs[0]["channel_id"], "101")


if __name__ == "__main__":
    unittest.main()
