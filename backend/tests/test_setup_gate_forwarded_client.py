"""
Regression test: allow_remote_setup=False must hold behind a Docker reverse proxy.

Bug: POST /api/auth/setup gated "local only" access on request.client.host via
_is_local_or_private_client(). Behind a reverse proxy in Docker (e.g. Unraid +
Nginx Proxy Manager) the socket peer is the proxy container's private RFC1918
address for EVERY request, so all forwarded traffic -- including remote internet
clients -- was treated as local and could complete initial admin setup before a
password was set.

Fix: when forwarding headers (X-Forwarded-For / X-Real-IP) are present, the
effective client is the forwarded address (right-most X-Forwarded-For entry,
the one appended by the nearest proxy), and it must be local/private too.
Forwarded headers are only consulted when the direct peer is itself
local/private, so a public peer cannot spoof its way in. Genuine direct
localhost/LAN access (no proxy headers) keeps working, and
allow_remote_setup=True still bypasses the gate entirely.
"""
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import auth as auth_api
from api.auth import _setup_client_allowed, setup_auth


class _CIDict(dict):
    """Case-insensitive dict simulating Starlette Headers.get() behavior."""

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


def _make_request(
    client_host: str | None,
    forwarded_for: str | None = None,
    real_ip: str | None = None,
):
    request = MagicMock()
    if client_host is None:
        request.client = None
    else:
        client = MagicMock()
        client.host = client_host
        request.client = client
    headers: _CIDict = _CIDict()
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    if real_ip is not None:
        headers["x-real-ip"] = real_ip
    request.headers = headers
    return request


DOCKER_PROXY_IP = "172.18.0.2"  # NPM container on the Docker bridge network
# Verifiably public client address. Note: the TEST-NET documentation ranges
# (e.g. 203.0.113.0/24) are treated as *private* by Python's ipaddress module,
# so they must not be used as the "remote internet client" here.
REMOTE_IP = "8.8.8.8"


class SetupGateForwardedClientTests(unittest.TestCase):
    def test_direct_localhost_allowed(self):
        """Direct loopback connection with no proxy headers stays allowed."""
        request = _make_request("127.0.0.1")
        self.assertTrue(_setup_client_allowed(request))

    def test_direct_lan_client_allowed(self):
        """Direct private-LAN connection with no proxy headers stays allowed."""
        request = _make_request("192.168.1.50")
        self.assertTrue(_setup_client_allowed(request))

    def test_direct_public_client_denied(self):
        """Direct public-internet connection is denied (pre-existing behavior)."""
        request = _make_request(REMOTE_IP)
        self.assertFalse(_setup_client_allowed(request))

    def test_docker_proxy_forwarded_remote_denied(self):
        """A remote internet client behind NPM-in-Docker must be denied.

        The socket peer is the proxy's private Docker bridge IP, but
        X-Forwarded-For carries the real (public) client address. Treating
        the private peer as 'local' let anyone on the internet claim the
        instance before an admin password was set.
        """
        request = _make_request(DOCKER_PROXY_IP, forwarded_for=REMOTE_IP)
        self.assertFalse(
            _setup_client_allowed(request),
            "Forwarded public client behind a Docker reverse proxy must not "
            "be treated as a trusted local source",
        )

    def test_docker_proxy_forwarded_lan_client_allowed(self):
        """A LAN admin going through the local reverse proxy keeps working."""
        request = _make_request(DOCKER_PROXY_IP, forwarded_for="192.168.1.50")
        self.assertTrue(_setup_client_allowed(request))

    def test_forwarded_remote_allowed_when_remote_setup_enabled(self):
        """allow_remote_setup=True still bypasses the local-only gate."""
        request = _make_request(DOCKER_PROXY_IP, forwarded_for=REMOTE_IP)
        with mock.patch.object(auth_api.settings, "allow_remote_setup", True):
            self.assertTrue(_setup_client_allowed(request))

    def test_x_real_ip_remote_denied(self):
        """X-Real-IP alone (no X-Forwarded-For) also marks the request proxied."""
        request = _make_request(DOCKER_PROXY_IP, real_ip=REMOTE_IP)
        self.assertFalse(_setup_client_allowed(request))

    def test_x_real_ip_lan_client_allowed(self):
        request = _make_request(DOCKER_PROXY_IP, real_ip="10.0.0.5")
        self.assertTrue(_setup_client_allowed(request))

    def test_multi_hop_xff_uses_rightmost_entry(self):
        """Only the right-most (proxy-appended) X-Forwarded-For entry counts.

        A remote attacker can put 127.0.0.1 in the left-most position; the
        proxy appends the attacker's real address as the right-most entry.
        """
        request = _make_request(
            DOCKER_PROXY_IP, forwarded_for=f"127.0.0.1, {REMOTE_IP}"
        )
        self.assertFalse(_setup_client_allowed(request))

    def test_spoofed_forwarded_local_from_public_peer_denied(self):
        """A public peer spoofing X-Forwarded-For: 127.0.0.1 must stay denied.

        Forwarded headers are only consulted when the direct peer is itself
        local/private; otherwise an internet client connecting directly to an
        exposed instance could forge a 'local' identity.
        """
        request = _make_request(REMOTE_IP, forwarded_for="127.0.0.1")
        self.assertFalse(_setup_client_allowed(request))

    def test_empty_forwarded_header_fails_closed(self):
        """A present-but-empty X-Forwarded-For marks the request proxied and
        provides no client address, so the gate fails closed."""
        request = _make_request(DOCKER_PROXY_IP, forwarded_for="")
        self.assertFalse(_setup_client_allowed(request))

    def test_missing_client_denied(self):
        request = _make_request(None)
        self.assertFalse(_setup_client_allowed(request))

    def test_setup_auth_uses_gate(self):
        """Wiring guard: the /api/auth/setup handler must call the gate."""
        source = inspect.getsource(setup_auth)
        self.assertIn(
            "_setup_client_allowed",
            source,
            "setup_auth must gate access through _setup_client_allowed",
        )


if __name__ == "__main__":
    unittest.main()
