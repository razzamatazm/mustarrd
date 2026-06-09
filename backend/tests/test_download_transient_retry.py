"""
Regression: a transient network error (connection reset, read timeout)
mid-download permanently failed the download.

Root cause: _download_file performed a single GET; any aiohttp transport error
raised during streaming propagated straight to _execute_download's exception
handler, which marked the download FAILED and unlinked the partial file.

Fix: _download_file is now a bounded-retry wrapper around _download_file_once.
Transient network errors (aiohttp.ClientError transport failures, timeouts)
are retried up to TRANSIENT_RETRY_LIMIT times with backoff, resuming from the
bytes already on disk via HTTP Range. Non-network errors (HTTP status errors,
disk errors) still fail fast.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import (
    DownloadManager,
    TRANSIENT_RETRY_LIMIT,
    _is_transient_network_error,
)


def _make_response(status, chunks, content_range=None, content_length=None,
                   fail_after=None):
    """Mock aiohttp response. If fail_after is an exception instance, it is
    raised from iter_chunked after the chunks are exhausted (mid-body error)."""
    response = MagicMock()
    response.status = status
    response.content_length = content_length
    response.reason = "OK"
    headers = {}
    if content_range:
        headers["Content-Range"] = content_range
    response.headers = headers

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk
        if fail_after is not None:
            raise fail_after

    response.content.iter_chunked = iter_chunked
    return response


def _make_client_session_multi(responses_or_errors):
    """Mock aiohttp.ClientSession whose .get() yields each item in turn.
    An Exception instance raises on connect (e.g. ClientConnectorError)."""
    call_index = [0]
    captured_headers = []

    def get_side_effect(*args, **kwargs):
        captured_headers.append(kwargs.get("headers", {}))
        idx = call_index[0]
        call_index[0] += 1
        item = responses_or_errors[idx]
        cm = MagicMock()
        if isinstance(item, BaseException):
            cm.__aenter__ = AsyncMock(side_effect=item)
        else:
            cm.__aenter__ = AsyncMock(return_value=item)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_session = MagicMock()
    mock_session.get.side_effect = get_side_effect

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=mock_session)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_session, client_cm, captured_headers


def _make_db_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


class TransientErrorClassifierTests(unittest.TestCase):
    def test_timeouts_and_transport_errors_are_transient(self):
        self.assertTrue(_is_transient_network_error(asyncio.TimeoutError()))
        self.assertTrue(_is_transient_network_error(aiohttp.ServerDisconnectedError()))
        self.assertTrue(_is_transient_network_error(aiohttp.ClientPayloadError("reset")))

    def test_http_and_disk_errors_are_not_transient(self):
        self.assertFalse(_is_transient_network_error(Exception("HTTP 404: Not Found")))
        self.assertFalse(_is_transient_network_error(OSError(28, "No space left on device")))
        self.assertFalse(
            _is_transient_network_error(
                aiohttp.ClientResponseError(MagicMock(), (), status=404)
            )
        )


class DownloadTransientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mid_body_disconnect_retries_and_resumes_with_range(self):
        """
        A ClientPayloadError after 1024 bytes must be retried. The second GET
        must carry Range: bytes=1024- and the 206 body must be appended, so the
        final file is the full content.
        """
        first_part = b"\x00" * 1024
        second_part = b"\x47" * 512

        response_fail = _make_response(
            200, [first_part], fail_after=aiohttp.ClientPayloadError("connection reset")
        )
        response_resume = _make_response(
            206, [second_part], content_range="bytes 1024-1535/1536"
        )
        mock_session, client_cm, headers = _make_client_session_multi(
            [response_fail, response_resume]
        )

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch("services.download_manager.TRANSIENT_RETRY_BACKOFF_SECONDS", 0),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=0
                )

            self.assertEqual(mock_session.get.call_count, 2, "One retry expected.")
            self.assertEqual(
                headers[1].get("Range"),
                "bytes=1024-",
                "Retry must resume from the bytes already on disk via Range.",
            )
            self.assertEqual(total, 1536)
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), first_part + second_part)
        finally:
            os.unlink(tmp_path)

    async def test_connect_error_retries_until_success(self):
        """A connection error on GET (before any body) must be retried."""
        body = b"\x47" * 256
        connect_err = aiohttp.ClientConnectionError("connection refused")
        response_ok = _make_response(200, [body])
        mock_session, client_cm, _headers = _make_client_session_multi(
            [connect_err, response_ok]
        )

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch("services.download_manager.TRANSIENT_RETRY_BACKOFF_SECONDS", 0),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._download_file(
                    "http://provider/stream", tmp_path, 1, db_session, offset=0
                )

            self.assertEqual(mock_session.get.call_count, 2)
            self.assertEqual(total, 256)
        finally:
            os.unlink(tmp_path)

    async def test_http_error_fails_fast_without_retry(self):
        """HTTP 404 is fatal: no retry, single GET."""
        response_404 = _make_response(404, [])
        response_404.reason = "Not Found"
        mock_session, client_cm, _headers = _make_client_session_multi([response_404])

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch("services.download_manager.TRANSIENT_RETRY_BACKOFF_SECONDS", 0),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                with self.assertRaises(Exception) as ctx:
                    await manager._download_file(
                        "http://provider/stream", tmp_path, 1, db_session, offset=0
                    )
            self.assertIn("HTTP 404", str(ctx.exception))
            self.assertEqual(
                mock_session.get.call_count, 1,
                "HTTP status errors must fail fast without retry.",
            )
        finally:
            os.unlink(tmp_path)

    async def test_retries_are_bounded(self):
        """After TRANSIENT_RETRY_LIMIT retries the last error propagates."""
        errors = [
            aiohttp.ClientConnectionError("refused")
            for _ in range(TRANSIENT_RETRY_LIMIT + 1)
        ]
        mock_session, client_cm, _headers = _make_client_session_multi(errors)

        manager = DownloadManager()
        db_session = _make_db_session()

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch("services.download_manager.aiohttp.ClientSession", return_value=client_cm),
                patch("services.download_manager.TRANSIENT_RETRY_BACKOFF_SECONDS", 0),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await manager._download_file(
                        "http://provider/stream", tmp_path, 1, db_session, offset=0
                    )
            self.assertEqual(
                mock_session.get.call_count,
                TRANSIENT_RETRY_LIMIT + 1,
                "Initial attempt plus TRANSIENT_RETRY_LIMIT retries, then give up.",
            )
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
