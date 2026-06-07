"""
Regression test for _maybe_decompress raising uncaught gzip.BadGzipFile on
corrupt or truncated XMLTV.gz data from a provider.

When a provider returns a response whose body starts with the gzip magic bytes
(\x1f\x8b) but the rest of the payload is corrupt or truncated (e.g. a
mid-transfer network drop, a provider restart, or a proxy that returned an
HTML error page with a gzip header), gzip.decompress() raises BadGzipFile or
EOFError. Neither is caught in _maybe_decompress, so the entire EPG refresh
for that account fails silently. The connection status is marked unhealthy even
though the provider may be fine; the EPG guide goes stale indefinitely with no
user-visible error.

Expected behaviour after fix: _maybe_decompress catches the gzip exception and
returns b"" so the caller can treat it as "no XMLTV data" and log accordingly,
rather than aborting the refresh.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


class MaybeDecompressCorruptGzipTests(unittest.TestCase):

    def setUp(self):
        self.manager = EPGIngestManager()

    def test_corrupt_gzip_body_returns_empty(self):
        """_maybe_decompress must return b"" on corrupt gzip data, not raise BadGzipFile."""
        corrupt = b"\x1f\x8b" + b"\x00" * 20
        result = self.manager._maybe_decompress(corrupt)
        self.assertEqual(result, b"")

    def test_truncated_gzip_body_returns_empty(self):
        """_maybe_decompress must return b"" when data is truncated after magic bytes."""
        truncated = b"\x1f\x8b"
        result = self.manager._maybe_decompress(truncated)
        self.assertEqual(result, b"")

    def test_valid_gzip_still_decompresses(self):
        """_maybe_decompress must still decompress valid gzip data after fix."""
        import gzip
        payload = b"<?xml version='1.0'?><tv></tv>"
        compressed = gzip.compress(payload)
        result = self.manager._maybe_decompress(compressed)
        self.assertEqual(result, payload)

    def test_plain_xml_passes_through_unchanged(self):
        """_maybe_decompress must leave non-gzip data alone."""
        plain = b"<?xml version='1.0'?><tv></tv>"
        result = self.manager._maybe_decompress(plain)
        self.assertEqual(result, plain)


if __name__ == "__main__":
    unittest.main()
