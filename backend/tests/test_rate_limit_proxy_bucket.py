"""
Regression test for auth rate limiter sharing bucket behind reverse proxy.

Bug (LOW-MEDIUM): _enforce_rate_limit() uses request.client.host as the bucket
key. Behind Nginx Proxy Manager on Unraid, request.client.host is always the
Docker bridge IP (e.g. 172.18.0.2) for every user -- all proxied clients share
one bucket.

When Alice makes 20 login attempts the (login, 172.18.0.2) bucket fills.
Bob, connecting via the same NPM proxy for the first time, immediately gets
HTTP 429 even though he has never made a single request.

Fix required: use X-Forwarded-For (or X-Real-IP) when the direct peer is a
private/loopback address, so each real client has its own independent bucket.
Trust the forwarded header only when it arrives from a known-private peer so
the fix cannot be exploited by spoofing the header from the public internet.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from api.auth import _enforce_rate_limit, _attempt_log


class _CIDict(dict):
    """Case-insensitive dict that simulates FastAPI Headers.get() behavior.

    FastAPI's Headers object stores keys lowercase and lowercases the lookup
    key in .get(), so both .get("X-Forwarded-For") and .get("x-forwarded-for")
    return the same value. Using a plain dict here would reject the capitalized
    form even when the fix is correct in production.
    """

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


def _make_request(client_host: str, forwarded_for: str | None = None):
    """Return a minimal mock Request with the given client host and optional
    X-Forwarded-For header value."""
    request = MagicMock()
    client = MagicMock()
    client.host = client_host
    request.client = client
    headers: _CIDict = _CIDict()
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    request.headers = headers
    return request


class RateLimitProxyBucketTests(unittest.TestCase):
    def setUp(self):
        _attempt_log.clear()

    def tearDown(self):
        _attempt_log.clear()

    def test_distinct_forwarded_ips_get_independent_buckets(self):
        """Two real IPs behind the same proxy must not share a rate limit bucket.

        Bug: _client_key() returns request.client.host (the proxy's Docker
        bridge IP). Alice's 20 requests fill the (login, 172.18.0.2) bucket.
        Bob's first request, also arriving via the proxy, is rejected with
        429 even though he has made zero prior attempts.

        Expected (post-fix): _enforce_rate_limit reads X-Forwarded-For when the
        direct peer is a private address, so Bob's real IP (10.0.0.11) gets its
        own empty bucket and his first request succeeds.

        This test FAILS while the bug is present and passes after the fix.
        """
        PROXY_IP = "172.18.0.2"  # Docker bridge (NPM's address as seen by app)

        # Alice (real IP 10.0.0.10) exhausts the login quota through the proxy.
        for _ in range(20):
            req = _make_request(client_host=PROXY_IP, forwarded_for="10.0.0.10")
            _enforce_rate_limit("login", req)

        # Bob (real IP 10.0.0.11) makes his first login attempt via the same proxy.
        # His X-Forwarded-For is distinct from Alice's; he should not be limited.
        bob_req = _make_request(client_host=PROXY_IP, forwarded_for="10.0.0.11")
        try:
            _enforce_rate_limit("login", bob_req)
        except HTTPException as exc:
            if exc.status_code == 429:
                self.fail(
                    "Bob was rate-limited after zero attempts. "
                    "_client_key() used the proxy IP (172.18.0.2) instead of "
                    "Bob's real IP from X-Forwarded-For (10.0.0.11), so all "
                    "proxied clients share one bucket. "
                    "Fix: prefer X-Forwarded-For when request.client.host is a "
                    "private address."
                )
            raise

    def test_direct_connections_still_have_independent_buckets(self):
        """Without a proxy (direct TCP) each IP already gets its own bucket.

        This is a regression guard: the fix must not break the non-proxy case.
        """
        # Alice (direct, 192.168.1.10) fills her bucket.
        for _ in range(20):
            req = _make_request(client_host="192.168.1.10")
            _enforce_rate_limit("login", req)

        # Bob (direct, 192.168.1.11) must NOT be limited.
        bob_req = _make_request(client_host="192.168.1.11")
        try:
            _enforce_rate_limit("login", bob_req)
        except HTTPException:
            self.fail(
                "Bob should not be rate-limited when connecting directly "
                "with his own IP."
            )

    def test_same_real_ip_still_gets_limited(self):
        """A single real IP that exhausts the quota must still be blocked.

        This guards against the fix accidentally giving every request a fresh
        bucket (e.g. by using a timestamp or request-unique value as the key).
        """
        PROXY_IP = "172.18.0.2"
        for _ in range(20):
            req = _make_request(client_host=PROXY_IP, forwarded_for="10.0.0.10")
            _enforce_rate_limit("login", req)

        repeat_req = _make_request(
            client_host=PROXY_IP, forwarded_for="10.0.0.10"
        )
        with self.assertRaises(HTTPException) as ctx:
            _enforce_rate_limit("login", repeat_req)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_multi_hop_xff_uses_leftmost_ip(self):
        """Multi-hop X-Forwarded-For (client, proxy1, proxy2) must use the
        left-most IP as the real client address.

        Real XFF headers often look like "10.0.0.10, 172.18.0.1" when traffic
        passes through more than one proxy. A fix that grabs the whole string,
        the last entry, or splits incorrectly will bucket clients by the wrong
        IP. This test catches those mistakes.

        Alice and Bob have different real IPs but the same intermediate proxy
        hop in the header. Alice fills her bucket; Bob must not be blocked.
        """
        PROXY_IP = "172.18.0.2"

        # Alice's XFF: real client is 10.0.0.10, intermediate hop is 172.18.0.1.
        for _ in range(20):
            req = _make_request(
                client_host=PROXY_IP, forwarded_for="10.0.0.10, 172.18.0.1"
            )
            _enforce_rate_limit("login", req)

        # Bob's XFF: real client is 10.0.0.11, same intermediate hop.
        # If the fix grabs the whole string, or the rightmost entry (172.18.0.1),
        # Bob will share Alice's bucket and be blocked after zero attempts.
        bob_req = _make_request(
            client_host=PROXY_IP, forwarded_for="10.0.0.11, 172.18.0.1"
        )
        try:
            _enforce_rate_limit("login", bob_req)
        except HTTPException as exc:
            if exc.status_code == 429:
                self.fail(
                    "Bob was rate-limited in the multi-hop XFF case. "
                    "The fix must strip and use the left-most entry from "
                    "X-Forwarded-For, not the whole string or the rightmost hop."
                )
            raise


if __name__ == "__main__":
    unittest.main()
