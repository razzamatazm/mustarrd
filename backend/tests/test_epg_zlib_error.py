"""
Regression test for _maybe_decompress raising uncaught zlib.error on XMLTV
data with a valid gzip header but corrupt deflate body.

gzip.BadGzipFile and EOFError are raised when the gzip HEADER is malformed
or truncated. zlib.error is raised when the header is valid but the compressed
body is corrupt or truncated after the header. Both failure modes reach the
user the same way: the entire EPG refresh fails, the connection badge goes red,
and the guide goes stale silently.

The fix for gzip.BadGzipFile (PR #73) caught (gzip.BadGzipFile, EOFError,
OSError) but did NOT include zlib.error. A provider that sends a complete,
valid gzip header followed by garbage compressed data (e.g. from a mid-write
server restart, network corruption after the gzip frame begins, or a proxy
injecting an HTML error body after the gzip magic bytes) will still crash
_maybe_decompress after PR #73 is merged.

Expected behaviour after fix: _maybe_decompress catches zlib.error alongside
BadGzipFile/EOFError and returns b"" so the caller logs a warning rather than
aborting the refresh entirely.
"""
import gzip
import io
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_ingest_manager import EPGIngestManager


def _make_corrupt_deflate_body() -> bytes:
    """Build bytes with a syntactically valid gzip header but a garbage deflate body."""
    buf = io.BytesIO()
    # Write a valid 10-byte gzip header: magic, method=8, flags=0, mtime=0, xfl=0, OS=3
    buf.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03")
    # Write garbage where the deflate stream should be
    buf.write(b"\xff\xfe\xfd\xfc" * 20)
    return buf.getvalue()


class MaybeDecompressZlibErrorTests(unittest.TestCase):

    def setUp(self):
        self.manager = EPGIngestManager()

    def test_corrupt_deflate_body_does_not_raise(self):
        """_maybe_decompress must not propagate zlib.error on a corrupt deflate body.

        This differs from the BadGzipFile case: the gzip HEADER parses fine,
        but the deflate stream that follows is garbage. gzip.decompress raises
        zlib.error (not BadGzipFile) in this case. The fix in PR #73 caught
        (gzip.BadGzipFile, EOFError, OSError) but omitted zlib.error, so this
        specific case still aborts the EPG refresh.
        """
        corrupt = _make_corrupt_deflate_body()
        # Verify our fixture actually triggers zlib.error (not BadGzipFile)
        import zlib
        with self.assertRaises(zlib.error):
            gzip.decompress(corrupt)
        result = self.manager._maybe_decompress(corrupt)
        self.assertIsInstance(result, bytes)
        self.assertEqual(result, b"")

    def test_valid_gzip_still_decompresses_after_zlib_fix(self):
        """_maybe_decompress must still decompress valid gzip after the zlib fix."""
        payload = b"<?xml version='1.0'?><tv></tv>"
        compressed = gzip.compress(payload)
        result = self.manager._maybe_decompress(compressed)
        self.assertEqual(result, payload)


if __name__ == "__main__":
    unittest.main()
