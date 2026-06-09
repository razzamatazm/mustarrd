"""
Regression test: deflate-compressed XMLTV causes ParseError, guide silently stales.

Bug (LOW-MEDIUM): _maybe_decompress only handles gzip (checks for \\x1f\\x8b magic bytes).
When a provider serves XMLTV with Content-Encoding: deflate (zlib framing, RFC 1950 or
raw deflate RFC 1951), the compressed bytes are returned as-is. ET.iterparse receives
binary garbage and raises ParseError. The exception propagates to _refresh_all_accounts,
which marks the account connection-failed and logs an error.

Impact (non-force mode): per-channel expired rows are deleted before parsing. Parse
fails immediately, no new rows inserted. Guide grows stale silently; after archive_days
passes, all content disappears with no clear user-visible error.

Impact (force mode): deletion guard at line 275 checks programme count on compressed
bytes (always 0), so rows are preserved but the guide is still never updated.

Expected behavior (after fix): deflate-compressed XMLTV is transparently decompressed
before parsing, exactly like gzip-compressed XMLTV.

Fix: after the gzip check in _maybe_decompress, try zlib.decompress(data) for
zlib-framed deflate (RFC 1950), then zlib.decompress(data, -15) for raw deflate
(RFC 1951). Both fall back gracefully on zlib.error so uncompressed XML passes
through unchanged.
"""

import sys
import unittest
import zlib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager

_XMLTV_PAYLOAD = b"<?xml version='1.0' encoding='utf-8'?><tv></tv>"


def _zlib_compress(data: bytes) -> bytes:
    """Produce zlib-framed deflate (RFC 1950). This is what most servers mean by deflate."""
    obj = zlib.compressobj(wbits=15)
    return obj.compress(data) + obj.flush()


def _raw_deflate_compress(data: bytes) -> bytes:
    """Produce raw deflate (RFC 1951). Some servers send this despite the deflate label."""
    obj = zlib.compressobj(wbits=-15)
    return obj.compress(data) + obj.flush()


class DeflateDecompressTests(unittest.TestCase):
    """_maybe_decompress must transparently inflate deflate-compressed XMLTV."""

    def setUp(self):
        self.manager = EPGIngestManager()

    def test_zlib_framed_deflate_decompressed(self):
        """Zlib-framed deflate (RFC 1950, most common deflate encoding) must be decompressed.

        Bug: _maybe_decompress only checks for gzip magic bytes. A provider serving
        Content-Encoding: deflate with zlib framing returns raw compressed bytes to the
        caller. ET.iterparse receives binary garbage and raises ParseError.

        After fix: _maybe_decompress tries zlib.decompress(data) and returns the
        decompressed XML bytes.
        """
        compressed = _zlib_compress(_XMLTV_PAYLOAD)
        # Confirm the fixture does NOT have gzip magic bytes
        self.assertNotEqual(compressed[:2], b"\x1f\x8b",
                            "Fixture must not be gzip-framed for this test to be meaningful.")
        result = self.manager._maybe_decompress(compressed)
        self.assertEqual(
            result,
            _XMLTV_PAYLOAD,
            "Zlib-framed deflate must be transparently decompressed. "
            "Bug: _maybe_decompress returns compressed bytes when no gzip magic byte is found, "
            "causing ET.iterparse to raise ParseError on the binary garbage.",
        )

    def test_raw_deflate_decompressed(self):
        """Raw deflate (RFC 1951, no zlib header) must also be decompressed.

        Some servers send raw deflate despite RFC 2616 recommending zlib framing.
        After fix: _maybe_decompress falls back to zlib.decompress(data, -15) when
        the zlib-framed attempt fails.
        """
        compressed = _raw_deflate_compress(_XMLTV_PAYLOAD)
        self.assertNotEqual(compressed[:2], b"\x1f\x8b",
                            "Fixture must not be gzip-framed.")
        result = self.manager._maybe_decompress(compressed)
        self.assertEqual(
            result,
            _XMLTV_PAYLOAD,
            "Raw deflate (no zlib header) must be decompressed. "
            "Bug: _maybe_decompress returns raw compressed bytes, causing ParseError.",
        )

    def test_plain_xml_passes_through_unchanged(self):
        """Uncompressed XML must pass through unchanged (regression guard)."""
        result = self.manager._maybe_decompress(_XMLTV_PAYLOAD)
        self.assertEqual(result, _XMLTV_PAYLOAD,
                         "Uncompressed XML must pass through _maybe_decompress unchanged.")

    def test_gzip_still_decompresses(self):
        """Existing gzip path must continue to work after the deflate fix (regression guard)."""
        import gzip
        compressed = gzip.compress(_XMLTV_PAYLOAD)
        result = self.manager._maybe_decompress(compressed)
        self.assertEqual(result, _XMLTV_PAYLOAD,
                         "Gzip decompression must still work after the deflate fix.")

    def test_corrupt_deflate_returns_empty(self):
        """Corrupt gzip data (existing test) must still return b\"\" after deflate handling.

        The new zlib.decompress attempts will fail on gzip-magic-byte data that is
        corrupt; verify the corruption path still silently returns b\"\".
        """
        import gzip
        import io
        buf = io.BytesIO()
        buf.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03")
        buf.write(b"\xff\xfe\xfd\xfc" * 20)
        result = self.manager._maybe_decompress(buf.getvalue())
        self.assertEqual(result, b"",
                         "Corrupt gzip-framed data must still return b\"\" (existing behavior).")


if __name__ == "__main__":
    unittest.main()
