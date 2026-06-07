import asyncio
import errno
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import _friendly_error


class FriendlyErrorTests(unittest.TestCase):
    def test_timeout_error(self):
        msg = _friendly_error(asyncio.TimeoutError())
        self.assertIn("timed out", msg.lower())

    def test_client_connector_error(self):
        err = aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("Connection refused")
        )
        msg = _friendly_error(err)
        self.assertIn("cannot reach", msg.lower())

    def test_server_disconnected_error(self):
        msg = _friendly_error(aiohttp.ServerDisconnectedError())
        self.assertIn("disconnected", msg.lower())

    def test_generic_aiohttp_client_error(self):
        msg = _friendly_error(aiohttp.ClientError("some network error"))
        self.assertIn("network error", msg.lower())

    def test_http_401(self):
        msg = _friendly_error(Exception("HTTP 401: Unauthorized"))
        self.assertIn("credentials", msg.lower())

    def test_http_403(self):
        msg = _friendly_error(Exception("HTTP 403: Forbidden"))
        self.assertIn("credentials", msg.lower())

    def test_http_404(self):
        msg = _friendly_error(Exception("HTTP 404: Not Found"))
        self.assertIn("not found", msg.lower())

    def test_http_503(self):
        msg = _friendly_error(Exception("HTTP 503: Service Unavailable"))
        self.assertIn("server error", msg.lower())

    def test_http_other(self):
        msg = _friendly_error(Exception("HTTP 429: Too Many Requests"))
        self.assertIn("HTTP 429", msg)

    def test_disk_full(self):
        err = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        msg = _friendly_error(err)
        self.assertIn("disk space", msg.lower())

    def test_empty_exception_string(self):
        msg = _friendly_error(Exception(""))
        self.assertIn("unknown reason", msg.lower())

    def test_already_friendly_message_passes_through(self):
        friendly = (
            "Provider returned an empty response. "
            "The catchup window for this program may have expired."
        )
        msg = _friendly_error(Exception(friendly))
        self.assertEqual(msg, friendly)

    def test_missing_file_message_passes_through(self):
        original = "Missing input file for post-processing."
        msg = _friendly_error(Exception(original))
        self.assertEqual(msg, original)


if __name__ == "__main__":
    unittest.main()
