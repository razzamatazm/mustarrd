"""
Unit tests for _friendly_connection_error in epg_ingest_manager.

Covers all 7 branches:
  1. asyncio.TimeoutError
  2. aiohttp.ClientConnectorError
  3. aiohttp.ClientError (generic)
  4. "Invalid credentials" in str(e)
  5. "Authentication failed" / "HTTP 401" / "HTTP 403"
  6. "HTTP 5..." in str(e)
  7. Fallthrough (unrecognised message, including length cap)

Ordering matters: isinstance checks run before substring checks, so
ClientConnectorError (a subclass of ClientError) must be tested before
the generic ClientError branch.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import aiohttp
from services.epg_ingest_manager import _friendly_connection_error


class FriendlyConnectionErrorTests(unittest.TestCase):

    def test_timeout_error(self):
        result = _friendly_connection_error(asyncio.TimeoutError())
        self.assertIn("timed out", result.lower())

    def test_client_connector_error(self):
        # ClientConnectorError requires an os.strerror-compatible mock
        exc = aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("refused")
        )
        result = _friendly_connection_error(exc)
        self.assertIn("reach", result.lower())

    def test_generic_client_error(self):
        exc = aiohttp.ClientError("something went wrong")
        result = _friendly_connection_error(exc)
        self.assertIn("network", result.lower())

    def test_invalid_credentials_substring(self):
        exc = Exception("Invalid credentials for account")
        result = _friendly_connection_error(exc)
        self.assertIn("credentials", result.lower())

    def test_http_401(self):
        exc = Exception("HTTP 401: Unauthorized")
        result = _friendly_connection_error(exc)
        self.assertIn("login", result.lower())

    def test_http_403(self):
        exc = Exception("HTTP 403: Forbidden")
        result = _friendly_connection_error(exc)
        self.assertIn("login", result.lower())

    def test_http_500(self):
        exc = Exception("HTTP 500: Internal Server Error")
        result = _friendly_connection_error(exc)
        self.assertIn("server error", result.lower())

    def test_http_503(self):
        exc = Exception("HTTP 503: Service Unavailable")
        result = _friendly_connection_error(exc)
        self.assertIn("server error", result.lower())

    def test_fallthrough_short_message(self):
        exc = Exception("some unrecognised error")
        result = _friendly_connection_error(exc)
        self.assertEqual(result, "some unrecognised error")

    def test_fallthrough_empty_message(self):
        exc = Exception("")
        result = _friendly_connection_error(exc)
        self.assertEqual(result, "Connection failed.")

    def test_fallthrough_truncates_long_message(self):
        long_msg = "x" * 300
        exc = Exception(long_msg)
        result = _friendly_connection_error(exc)
        self.assertLessEqual(len(result), 210, "Long messages must be truncated")
        self.assertTrue(result.endswith("..."))

    def test_connector_error_before_generic_client_error(self):
        """ClientConnectorError is a ClientError subclass: must hit the connector branch."""
        exc = aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("connection refused")
        )
        result = _friendly_connection_error(exc)
        self.assertIn("reach", result.lower(),
                      "ClientConnectorError must match the connector branch, not the generic one")


if __name__ == "__main__":
    unittest.main()
