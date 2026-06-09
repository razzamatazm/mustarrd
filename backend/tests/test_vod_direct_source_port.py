"""
Regression test: _trusted_direct_source must compare host AND port.

Bug: services/vod_service._trusted_direct_source validated only the hostname
of a provider-supplied direct_source URL against the account server URL. A URL
pointing at the same hostname but a DIFFERENT port (i.e. a different service on
the provider's machine, or a port chosen via a manipulated API response) passed
the trust check and was downloaded as-is.

Fix: the effective port must match too, resolving default ports (80 for http,
443 for https) so that an implicit default port and its explicit equivalent
still compare equal. Invalid ports fail closed instead of raising.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.vod_service import _trusted_direct_source


ACCOUNT_URL = "http://cdn.example.com:8080"


class TrustedDirectSourcePortTests(unittest.TestCase):
    def test_same_host_different_port_rejected(self):
        """Same hostname on another port is a different service: untrusted."""
        self.assertIsNone(
            _trusted_direct_source("http://cdn.example.com:9999/movie/1.mkv", ACCOUNT_URL)
        )

    def test_same_host_same_port_allowed(self):
        url = "http://cdn.example.com:8080/movie/1.mkv"
        self.assertEqual(_trusted_direct_source(url, ACCOUNT_URL), url)

    def test_implicit_vs_explicit_default_http_port_allowed(self):
        """http with no port and http on :80 are the same origin."""
        url = "http://cdn.example.com:80/movie/1.mkv"
        self.assertEqual(_trusted_direct_source(url, "http://cdn.example.com"), url)

    def test_implicit_vs_explicit_default_https_port_allowed(self):
        """https with no port and https on :443 are the same origin."""
        url = "https://cdn.example.com/movie/1.mkv"
        self.assertEqual(
            _trusted_direct_source(url, "https://cdn.example.com:443"), url
        )

    def test_default_port_vs_custom_port_rejected(self):
        """Account on implicit :80, source on :8080: different ports."""
        self.assertIsNone(
            _trusted_direct_source(
                "http://cdn.example.com:8080/movie/1.mkv", "http://cdn.example.com"
            )
        )

    def test_http_source_for_https_account_rejected_by_default_ports(self):
        """https account (443) vs http source (80) resolve to different ports."""
        self.assertIsNone(
            _trusted_direct_source(
                "http://cdn.example.com/movie/1.mkv", "https://cdn.example.com"
            )
        )

    def test_invalid_port_in_source_fails_closed(self):
        """A non-numeric port must be rejected, not raise ValueError."""
        self.assertIsNone(
            _trusted_direct_source("http://cdn.example.com:notaport/movie/1.mkv", ACCOUNT_URL)
        )

    def test_different_host_still_rejected(self):
        self.assertIsNone(
            _trusted_direct_source("http://evil.example.com:8080/movie/1.mkv", ACCOUNT_URL)
        )

    def test_non_http_scheme_still_rejected(self):
        self.assertIsNone(
            _trusted_direct_source("ftp://cdn.example.com:8080/movie/1.mkv", ACCOUNT_URL)
        )

    def test_missing_direct_source_returns_none(self):
        self.assertIsNone(_trusted_direct_source(None, ACCOUNT_URL))
        self.assertIsNone(_trusted_direct_source("", ACCOUNT_URL))


if __name__ == "__main__":
    unittest.main()
