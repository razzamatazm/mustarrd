"""
Regression tests for two XMLTV EPG ingest bugs.

Bug 1 (HIGH): XMLTV with default XML namespace imports zero EPG rows silently.
  When a provider's XMLTV feed carries a default namespace (xmlns="..."), Python's
  ElementTree tags every element with the namespace prefix:
  {http://xmltv.org/ns/}channel, {http://xmltv.org/ns/}programme.
  The string comparisons in _iter_programs check elem.tag == "channel" and
  elem.tag != "programme", so neither ever matches. Zero rows imported, no error.
  Existing TODO thread: #1512745542021546084

Bug 2 (MEDIUM): Named timezone strings (EST, PST, CET, etc.) silently stored as UTC.
  _parse_xmltv_time calls datetime.strptime(tz_part, "%z") which raises ValueError
  for named zones. The except clause returns dt.replace(tzinfo=timezone.utc), so
  a program at "20240115120000 EST" is stored as 12:00 UTC instead of 17:00 UTC.
  5-hour error; scheduled recordings fire against the wrong content.
  Existing TODO thread: #1512745635303129172
"""
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

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


# ---------------------------------------------------------------------------
# Bug 1: Default XML namespace
# ---------------------------------------------------------------------------

XMLTV_NO_NAMESPACE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv>'
    b'<channel id="bbc1"><display-name>BBC One</display-name></channel>'
    b'<programme start="20240101120000 +0000" stop="20240101130000 +0000" channel="bbc1">'
    b'<title>News at Noon</title>'
    b'</programme>'
    b'</tv>'
)

XMLTV_WITH_NAMESPACE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv xmlns="http://xmltv.org/ns/">'
    b'<channel id="bbc1"><display-name>BBC One</display-name></channel>'
    b'<programme start="20240101120000 +0000" stop="20240101130000 +0000" channel="bbc1">'
    b'<title>News at Noon</title>'
    b'</programme>'
    b'</tv>'
)

XMLTV_WITH_ALT_NAMESPACE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<tv xmlns="http://www.xmltv.org/DTD/xmltv">'
    b'<channel id="ch1"><display-name>CNN</display-name></channel>'
    b'<programme start="20240101140000 +0000" stop="20240101150000 +0000" channel="ch1">'
    b'<title>World News</title>'
    b'</programme>'
    b'</tv>'
)


class XmltvDefaultNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.manager = EPGIngestManager()
        self.now = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    def _collect(self, channels, xmltv_bytes):
        channel_maps = self.manager._build_channel_maps(channels)
        return list(self.manager._iter_programs(xmltv_bytes, channel_maps, self.now))

    def test_baseline_no_namespace_imports_programs(self):
        """Sanity: namespace-free XMLTV must yield programs."""
        channels = [_make_channel("101", "BBC One", epg_channel_id="bbc1")]
        programs = self._collect(channels, XMLTV_NO_NAMESPACE)
        self.assertGreater(len(programs), 0, "No programs from namespace-free XMLTV.")

    def test_default_namespace_imports_programs(self):
        """XMLTV with xmlns= default namespace must still yield programs.

        Bug: elem.tag becomes '{http://xmltv.org/ns/}channel' / '{...}programme',
        neither matches the plain string comparisons, so zero rows are imported.
        """
        channels = [_make_channel("101", "BBC One", epg_channel_id="bbc1")]
        programs = self._collect(channels, XMLTV_WITH_NAMESPACE)
        self.assertGreater(
            len(programs),
            0,
            "XMLTV with a default XML namespace imported zero programs. "
            "ElementTree qualifies tag names with the namespace prefix, "
            "but _iter_programs compares against bare 'channel'/'programme' strings. "
            "Fix: strip namespace from elem.tag before comparison.",
        )

    def test_alt_default_namespace_imports_programs(self):
        """Second common XMLTV namespace URI must also yield programs."""
        channels = [_make_channel("101", "CNN", epg_channel_id="ch1")]
        programs = self._collect(channels, XMLTV_WITH_ALT_NAMESPACE)
        self.assertGreater(
            len(programs),
            0,
            "XMLTV with xmlns='http://www.xmltv.org/DTD/xmltv' imported zero programs.",
        )

    def test_default_namespace_same_count_as_no_namespace(self):
        """Program count with namespace must equal count without namespace."""
        channels = [_make_channel("101", "BBC One", epg_channel_id="bbc1")]
        no_ns = self._collect(channels, XMLTV_NO_NAMESPACE)
        with_ns = self._collect(channels, XMLTV_WITH_NAMESPACE)
        self.assertEqual(
            len(no_ns),
            len(with_ns),
            f"Without namespace: {len(no_ns)} programs. With namespace: {len(with_ns)} programs. "
            "The namespace variant should produce identical results.",
        )


# ---------------------------------------------------------------------------
# Bug 2: Named timezone strings silently treated as UTC
# ---------------------------------------------------------------------------

class XmltvNamedTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.manager = EPGIngestManager.__new__(EPGIngestManager)

    def test_est_returns_utc_minus_5(self):
        """'20240115120000 EST' must be stored as 17:00 UTC, not 12:00 UTC.

        Bug: datetime.strptime('EST', '%z') raises ValueError, caught and
        returns dt.replace(tzinfo=timezone.utc), storing 12:00 UTC instead.
        EST is UTC-5, so noon EST = 17:00 UTC.
        """
        result = self.manager._parse_xmltv_time("20240115120000 EST")
        self.assertIsNotNone(result, "EST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 1, 15, 17, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"EST noon should be 17:00 UTC, got {result_utc.isoformat()}. "
            "Named timezone 'EST' was silently treated as UTC.",
        )

    def test_pst_returns_utc_minus_8(self):
        """'20240115120000 PST' must be stored as 20:00 UTC (UTC-8)."""
        result = self.manager._parse_xmltv_time("20240115120000 PST")
        self.assertIsNotNone(result, "PST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 1, 15, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"PST noon should be 20:00 UTC, got {result_utc.isoformat()}. "
            "Named timezone 'PST' was silently treated as UTC.",
        )

    def test_cet_returns_utc_plus_1(self):
        """'20240115120000 CET' must be stored as 11:00 UTC (UTC+1)."""
        result = self.manager._parse_xmltv_time("20240115120000 CET")
        self.assertIsNotNone(result, "CET timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"CET noon should be 11:00 UTC, got {result_utc.isoformat()}. "
            "Named timezone 'CET' was silently treated as UTC.",
        )

    def test_edt_returns_utc_minus_4(self):
        """'20240615120000 EDT' (Eastern Daylight Time, UTC-4) must be 16:00 UTC."""
        result = self.manager._parse_xmltv_time("20240615120000 EDT")
        self.assertIsNotNone(result, "EDT timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 6, 15, 16, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"EDT noon should be 16:00 UTC, got {result_utc.isoformat()}. "
            "Named timezone 'EDT' was silently treated as UTC.",
        )

    def test_numeric_offset_still_works(self):
        """Numeric offset '-0500' must still parse correctly (regression guard)."""
        result = self.manager._parse_xmltv_time("20240115120000 -0500")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 1, 15, 17, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(result_utc, expected_utc)

    def test_est_not_stored_as_utc(self):
        """EST must NOT be treated as UTC (the specific broken behavior to prevent)."""
        result = self.manager._parse_xmltv_time("20240115120000 EST")
        self.assertIsNotNone(result)
        result_utc = result.astimezone(timezone.utc)
        wrong_utc = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.assertNotEqual(
            result_utc,
            wrong_utc,
            "EST was stored as UTC (12:00 UTC). Named timezone not handled: "
            "datetime.strptime('EST', '%z') raised ValueError and fell through "
            "to dt.replace(tzinfo=timezone.utc).",
        )

    def test_bst_returns_utc_minus_1(self):
        """'20240615120000 BST' (British Summer Time, UTC+1) must be stored as 11:00 UTC.

        UK providers use BST in summer-dated XMLTV. Without this entry, BST falls
        through to strptime('%z'), raises ValueError, and is stored as UTC: 1 hour off
        for the entire UK summer schedule.
        """
        result = self.manager._parse_xmltv_time("20240615120000 BST")
        self.assertIsNotNone(result, "BST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 6, 15, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"BST noon should be 11:00 UTC, got {result_utc.isoformat()}. "
            "Named timezone 'BST' was silently treated as UTC.",
        )

    def test_wet_returns_utc(self):
        """'20240115120000 WET' (Western European Time, UTC+0) must be 12:00 UTC."""
        result = self.manager._parse_xmltv_time("20240115120000 WET")
        self.assertIsNotNone(result, "WET timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"WET noon should be 12:00 UTC, got {result_utc.isoformat()}.",
        )

    def test_west_returns_utc_minus_1(self):
        """'20240615120000 WEST' (Western European Summer Time, UTC+1) must be 11:00 UTC."""
        result = self.manager._parse_xmltv_time("20240615120000 WEST")
        self.assertIsNotNone(result, "WEST timestamp returned None.")
        result_utc = result.astimezone(timezone.utc)
        expected_utc = datetime(2024, 6, 15, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            result_utc,
            expected_utc,
            f"WEST noon should be 11:00 UTC, got {result_utc.isoformat()}.",
        )


if __name__ == "__main__":
    unittest.main()
