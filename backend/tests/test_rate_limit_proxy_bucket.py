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

    def test_spoofed_xff_from_public_peer_is_ignored(self):
        """X-Forwarded-For from a public-internet peer must be ignored.

        An attacker connecting directly from a public IP can set any value in
        X-Forwarded-For. If the fix trusted XFF regardless of the direct peer,
        the attacker could forge X-Forwarded-For: 10.0.0.X on every request to
        mint an unlimited supply of fresh buckets, bypassing the rate limit.

        Fix: XFF is trusted only when request.client.host is a private/loopback
        address. Public peers are bucketed by their real IP directly.

        This test FAILS with a fix that blindly trusts XFF from all peers and
        passes with a correctly gated fix.
        """
        PUBLIC_IP = "8.8.8.8"  # Google DNS, verifiably public (not private or loopback)

        # Attacker makes 20 attempts with a forged XFF private address.
        # If XFF were trusted, each attempt uses a different private IP and
        # mints a fresh bucket, bypassing the limit entirely.
        for i in range(20):
            req = _make_request(client_host=PUBLIC_IP, forwarded_for=f"10.0.0.{i + 1}")
            _enforce_rate_limit("login", req)

        # 21st attempt from same public IP (different forged XFF) must be blocked.
        # If XFF is trusted: bucket for "10.0.0.21" is empty, no 429. Test fails.
        # If XFF is ignored: bucket for "203.0.113.5" is full, 429 raised. Test passes.
        repeat_req = _make_request(client_host=PUBLIC_IP, forwarded_for="10.0.0.21")
        with self.assertRaises(HTTPException) as ctx:
            _enforce_rate_limit("login", repeat_req)
        self.assertEqual(
            ctx.exception.status_code,
            429,
            "Public peer with forged X-Forwarded-For should be rate-limited "
            "by their real IP, not get a fresh bucket per forged header value.",
        )

    def test_multi_hop_xff_uses_rightmost_ip(self):
        """Multi-hop X-Forwarded-For must bucket by the right-most (proxy-appended) IP.

        Behind NPM the header looks like "<client-sent>, <npm-appended-client-ip>".
        NPM appends the IP that connected to it, so the right-most entry is the
        trustworthy one. The left-most is client-controlled and spoofable.

        Alice and Bob both connect via the proxy. They share the same client-sent
        (left-most) entry but have different proxy-appended (right-most) entries.
        Alice fills her bucket; Bob must not be blocked.

        With left-most keying: both share the same bucket, Bob blocked. Test FAILS.
        With right-most keying: independent buckets, Bob not blocked. Test passes.
        """
        PROXY_IP = "172.18.0.2"
        SHARED_SPOOFED = "192.0.2.1"  # same client-sent left-most for both

        # Alice: proxy appends her real IP 10.0.0.10 as right-most.
        for _ in range(20):
            req = _make_request(
                client_host=PROXY_IP, forwarded_for=f"{SHARED_SPOOFED}, 10.0.0.10"
            )
            _enforce_rate_limit("login", req)

        # Bob: proxy appends his real IP 10.0.0.11 as right-most.
        # Left-most is the same SHARED_SPOOFED; a left-most-keying fix buckets Bob
        # with Alice and blocks him after zero attempts.
        bob_req = _make_request(
            client_host=PROXY_IP, forwarded_for=f"{SHARED_SPOOFED}, 10.0.0.11"
        )
        try:
            _enforce_rate_limit("login", bob_req)
        except HTTPException as exc:
            if exc.status_code == 429:
                self.fail(
                    "Bob was rate-limited in the multi-hop XFF case. "
                    "The fix must use the right-most (proxy-appended) entry from "
                    "X-Forwarded-For, not the left-most (client-controlled) entry."
                )
            raise

    def test_through_proxy_spoofed_xff_left_entry_still_rate_limited(self):
        """Attacker varying the left-most XFF through a private proxy must still
        be rate-limited by their real (proxy-appended right-most) IP.

        An attacker behind NPM can forge any X-Forwarded-For value. NPM then
        appends the attacker's actual connecting IP as the right-most entry. If
        the fix keys on the left-most entry, the attacker mints a fresh bucket
        per request by varying that value, bypassing the limiter entirely.

        Fix: key on right-most (proxy-appended) entry. The attacker's real IP is
        always the right-most entry regardless of what they forge to the left.

        This test FAILS with a left-most-keying fix and passes with right-most.
        """
        PROXY_IP = "172.18.0.2"       # private peer: NPM Docker bridge
        ATTACKER_REAL_IP = "10.0.0.99"  # appended by NPM (right-most, not spoofable)

        # Attacker makes 20 requests varying the left-most entry each time.
        # With right-most keying: all 20 land in the ATTACKER_REAL_IP bucket.
        # With left-most keying: each gets a fresh bucket, limit never reached.
        for i in range(20):
            req = _make_request(
                client_host=PROXY_IP,
                forwarded_for=f"10.0.0.{i + 1}, {ATTACKER_REAL_IP}",
            )
            _enforce_rate_limit("login", req)

        # 21st request with another forged left entry must be blocked.
        spoof_req = _make_request(
            client_host=PROXY_IP,
            forwarded_for=f"10.0.0.21, {ATTACKER_REAL_IP}",
        )
        with self.assertRaises(HTTPException) as ctx:
            _enforce_rate_limit("login", spoof_req)
        self.assertEqual(
            ctx.exception.status_code,
            429,
            "Attacker varying left-most XFF through proxy must be rate-limited "
            "by their proxy-appended real IP (right-most entry), not the forged "
            "left-most value.",
        )


if __name__ == "__main__":
    unittest.main()
