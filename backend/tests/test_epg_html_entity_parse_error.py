"""
Regression: ET.iterparse rejects HTML entities not defined in XML (&nbsp;,
&eacute;, &mdash;, etc.). Providers that generate XMLTV from HTML include these
in <desc> or <title> elements.

Before fix: iterparse raised ParseError on &nbsp;; _iter_programs caught it
after yielding partial results. With force=True, rows were deleted before
iteration started, so a ParseError on the first programme left the guide empty.

After fix: _sanitize_html_entities() escapes non-standard named entities before
passing bytes to iterparse. ParseError never occurs; all programmes are ingested.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager, _sanitize_html_entities, _BARE_AMP_RE


_XMLTV_HTML_ENTITIES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<tv>"
    b'<channel id="ch1"><display-name>Test Channel</display-name></channel>'
    b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="ch1">'
    b"<title>Good Show</title>"
    b"</programme>"
    b'<programme start="20261201210000 +0000" stop="20261201220000 +0000" channel="ch1">'
    b"<title>HTML Show</title>"
    b"<desc>Watch it&nbsp;on Monday &mdash; don&apos;t miss it</desc>"
    b"</programme>"
    b"</tv>"
)

_CHANNEL_MAPS = {
    "stream_by_xmltv_id": {"ch1": "ch1"},
    "stream_by_name": {},
    "stream_info": {
        "ch1": {
            "name": "Test Channel",
            "has_archive": True,
            "archive_days": 30,
        }
    },
}

_NOW_UTC = datetime(2026, 12, 2, 0, 0, 0, tzinfo=timezone.utc)


class SanitizeHtmlEntitiesTests(unittest.TestCase):

    def test_nbsp_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"A&nbsp;B"), b"A&amp;nbsp;B")

    def test_mdash_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"A&mdash;B"), b"A&amp;mdash;B")

    def test_eacute_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"caf&eacute;"), b"caf&amp;eacute;")

    def test_xml_entities_preserved(self):
        for entity in [b"&amp;", b"&lt;", b"&gt;", b"&apos;", b"&quot;"]:
            with self.subTest(entity=entity):
                self.assertEqual(_sanitize_html_entities(entity), entity)

    def test_numeric_decimal_entity_preserved(self):
        # &#160; is the XML-valid numeric form of &nbsp;
        self.assertEqual(_sanitize_html_entities(b"&#160;"), b"&#160;")

    def test_numeric_hex_entity_preserved(self):
        self.assertEqual(_sanitize_html_entities(b"&#x00A0;"), b"&#x00A0;")

    def test_multiple_entities(self):
        result = _sanitize_html_entities(b"&nbsp;x&amp;y&mdash;z")
        self.assertEqual(result, b"&amp;nbsp;x&amp;y&amp;mdash;z")

    def test_untouched_when_no_html_entities(self):
        data = b"<desc>Normal &amp; text</desc>"
        self.assertEqual(_sanitize_html_entities(data), data)

    def test_bare_amp_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"R&B Music"), b"R&amp;B Music")

    def test_bare_amp_in_title(self):
        self.assertEqual(
            _sanitize_html_entities(b"Rock & Roll"),
            b"Rock &amp; Roll",
        )

    def test_bare_amp_at_start(self):
        self.assertEqual(_sanitize_html_entities(b"& the band"), b"&amp; the band")

    def test_bare_amp_at_end(self):
        self.assertEqual(_sanitize_html_entities(b"News &"), b"News &amp;")

    def test_amp_entity_preserved_after_bare_amp_fix(self):
        # &amp; must survive both passes unchanged.
        self.assertEqual(_sanitize_html_entities(b"A &amp; B"), b"A &amp; B")

    def test_numeric_decimal_entity_not_double_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"&#160;"), b"&#160;")

    def test_numeric_hex_entity_not_double_escaped(self):
        self.assertEqual(_sanitize_html_entities(b"&#x00A0;"), b"&#x00A0;")

    def test_mixed_bare_amp_and_named_entity(self):
        # "&" bare + "&nbsp;" named entity both handled in one call.
        result = _sanitize_html_entities(b"R&B &nbsp; Show")
        self.assertEqual(result, b"R&amp;B &amp;nbsp; Show")


class IterProgramsHtmlEntityTests(unittest.TestCase):

    def test_both_programmes_yielded(self):
        """
        Both programmes must be yielded when one description contains &nbsp;.

        Before fix: iterparse raised ParseError on &nbsp;; only programmes before
        the bad entity were returned (or none at all in force mode).
        After fix: both programmes are yielded without error.
        """
        manager = EPGIngestManager()
        programs = list(manager._iter_programs(_XMLTV_HTML_ENTITIES, _CHANNEL_MAPS, _NOW_UTC))
        self.assertEqual(
            len(programs),
            2,
            f"Expected 2 programmes, got {len(programs)}. "
            "HTML entity in description must not abort iteration.",
        )

    def test_programme_titles_correct(self):
        manager = EPGIngestManager()
        programs = list(manager._iter_programs(_XMLTV_HTML_ENTITIES, _CHANNEL_MAPS, _NOW_UTC))
        titles = {p["title"] for p in programs}
        self.assertIn("Good Show", titles)
        self.assertIn("HTML Show", titles)

    def test_no_parse_error_raised(self):
        import xml.etree.ElementTree as RealET
        manager = EPGIngestManager()
        try:
            list(manager._iter_programs(_XMLTV_HTML_ENTITIES, _CHANNEL_MAPS, _NOW_UTC))
        except RealET.ParseError as exc:
            self.fail(
                f"_iter_programs raised ParseError for &nbsp; in description: {exc}"
            )

    def test_entity_in_first_programme_still_yields(self):
        """Entity in the first (and only) programme: still must yield, not drop."""
        xmltv = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b"<tv>"
            b'<channel id="c1"><display-name>C</display-name></channel>'
            b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="c1">'
            b"<title>Only Show</title>"
            b"<desc>Airs&nbsp;tonight</desc>"
            b"</programme>"
            b"</tv>"
        )
        maps = {
            "stream_by_xmltv_id": {"c1": "c1"},
            "stream_by_name": {},
            "stream_info": {"c1": {"name": "C", "has_archive": True, "archive_days": 30}},
        }
        manager = EPGIngestManager()
        programs = list(manager._iter_programs(xmltv, maps, _NOW_UTC))
        self.assertEqual(
            len(programs),
            1,
            "Programme with HTML entity in description must be yielded, not dropped.",
        )

    def test_clean_xmltv_unaffected(self):
        """Programmes with no HTML entities are not changed by the fix."""
        clean = (
            b'<?xml version="1.0" encoding="UTF-8"?><tv>'
            b'<channel id="c1"><display-name>Chan</display-name></channel>'
            b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="c1">'
            b"<title>Normal Show</title><desc>Regular &amp; desc</desc>"
            b"</programme></tv>"
        )
        maps = {
            "stream_by_xmltv_id": {"c1": "c1"},
            "stream_by_name": {},
            "stream_info": {"c1": {"name": "Chan", "has_archive": True, "archive_days": 30}},
        }
        manager = EPGIngestManager()
        programs = list(manager._iter_programs(clean, maps, _NOW_UTC))
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["title"], "Normal Show")

    def test_bare_amp_in_title_no_parse_error(self):
        """
        Bare & in a title (e.g. R&B Music, AT&T Special) must not raise ParseError.

        Before fix: expat raised ParseError on bare & not followed by entity name;
        with force=True the guide was wiped before parsing started, leaving it empty.
        After fix: _sanitize_html_entities escapes bare & to &amp; before iterparse.
        """
        xmltv = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b"<tv>"
            b'<channel id="c1"><display-name>Music</display-name></channel>'
            b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="c1">'
            b"<title>R&amp;B Music</title>"
            b"</programme>"
            b'<programme start="20261201210000 +0000" stop="20261201220000 +0000" channel="c1">'
            b"<title>Rock &amp; Roll Hour</title>"
            b"<desc>AT&amp;T Special event tonight</desc>"
            b"</programme>"
            b"</tv>"
        )
        # The XMLTV above already has proper &amp; (valid XML). Now test the raw form
        # that providers actually emit, with literal bare &.
        xmltv_raw = xmltv.replace(b"&amp;", b"&")
        maps = {
            "stream_by_xmltv_id": {"c1": "c1"},
            "stream_by_name": {},
            "stream_info": {"c1": {"name": "Music", "has_archive": True, "archive_days": 30}},
        }
        manager = EPGIngestManager()
        import xml.etree.ElementTree as RealET
        try:
            programs = list(manager._iter_programs(xmltv_raw, maps, _NOW_UTC))
        except RealET.ParseError as exc:
            self.fail(f"_iter_programs raised ParseError for bare & in title/desc: {exc}")
        self.assertEqual(len(programs), 2, "Both programmes must be yielded despite bare & characters.")

    def test_bare_amp_title_value_correct(self):
        """Title with bare & is ingested with the & intact (stored as literal &)."""
        xmltv = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b"<tv>"
            b'<channel id="c1"><display-name>News</display-name></channel>'
            b'<programme start="20261201200000 +0000" stop="20261201210000 +0000" channel="c1">'
            b"<title>News & Events</title>"
            b"</programme>"
            b"</tv>"
        )
        maps = {
            "stream_by_xmltv_id": {"c1": "c1"},
            "stream_by_name": {},
            "stream_info": {"c1": {"name": "News", "has_archive": True, "archive_days": 30}},
        }
        manager = EPGIngestManager()
        programs = list(manager._iter_programs(xmltv, maps, _NOW_UTC))
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["title"], "News & Events")


if __name__ == "__main__":
    unittest.main()
