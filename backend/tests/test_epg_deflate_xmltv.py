"""
Regression tests: deflate-compressed XMLTV passes through _maybe_decompress
unchanged, causing ET.iterparse to raise ParseError on the binary garbage.

_maybe_decompress checks for gzip magic bytes (\x1f\x8b) only. HTTP deflate
(Content-Encoding: deflate) uses zlib framing with no fixed magic signature.
Deflate data is returned as-is. The caller passes it to ET.iterparse, which
fails immediately.

Impact: provider EPG silently stops updating. Per-channel expired rows are
deleted in non-force mode; no new rows inserted. Guide empties as old content
ages past archive_days with no clear user error.

Fix: after failing the gzip check, try zlib.decompress() (handles deflate with
zlib header) and zlib.decompress(data, -15) (raw deflate, no header).
"""
import sys
import unittest
import zlib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager

_PLAIN_XMLTV = b'<?xml version="1.0" encoding="UTF-8"?><tv></tv>'


class MaybeDecompressDeflateTests(unittest.TestCase):
    def setUp(self):
        self.mgr = EPGIngestManager()

    def test_zlib_deflate_with_header_is_decompressed(self):
        """_maybe_decompress must decompress zlib-framed deflate data."""
        compressed = zlib.compress(_PLAIN_XMLTV)
        # Verify it is NOT gzip (no \x1f\x8b magic)
        self.assertNotEqual(compressed[:2], b"\x1f\x8b")
        result = self.mgr._maybe_decompress(compressed)
        self.assertEqual(
            result,
            _PLAIN_XMLTV,
            "_maybe_decompress returned raw compressed bytes for zlib deflate; "
            "ET.iterparse will raise ParseError on this input.",
        )

    def test_raw_deflate_no_header_is_decompressed(self):
        """_maybe_decompress must decompress raw deflate (no zlib header)."""
        compressed = zlib.compress(_PLAIN_XMLTV)[2:-4]  # strip zlib header+checksum
        result = self.mgr._maybe_decompress(compressed)
        self.assertEqual(
            result,
            _PLAIN_XMLTV,
            "_maybe_decompress returned raw compressed bytes for raw deflate; "
            "ET.iterparse will raise ParseError on this input.",
        )

    def test_gzip_still_decompresses_after_deflate_support(self):
        """Existing gzip path must continue to work when deflate support is added."""
        import gzip

        compressed = gzip.compress(_PLAIN_XMLTV)
        result = self.mgr._maybe_decompress(compressed)
        self.assertEqual(result, _PLAIN_XMLTV)

    def test_plain_xml_still_passes_through(self):
        """Non-compressed data must pass through unchanged."""
        result = self.mgr._maybe_decompress(_PLAIN_XMLTV)
        self.assertEqual(result, _PLAIN_XMLTV)

    def test_deflate_bytes_are_not_valid_xml(self):
        """Sanity check: deflate output fed to ET.iterparse causes ParseError.

        This demonstrates why unhandled deflate is a real bug: the caller
        would receive a ParseError rather than parsed content.
        """
        import xml.etree.ElementTree as ET
        import io

        compressed = zlib.compress(_PLAIN_XMLTV)
        with self.assertRaises(ET.ParseError):
            list(ET.iterparse(io.BytesIO(compressed), events=("end",)))


if __name__ == "__main__":
    unittest.main()
