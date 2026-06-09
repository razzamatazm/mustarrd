"""
Regression tests for HTTP Range support on GET /api/downloads/{id}/file.

Browser playback (action=play) needs single-range support so <video> can seek
and start playing before the whole file has transferred:
  - valid bounded range  -> 206 + Content-Range + exactly the requested bytes
  - open-ended range     -> 206 + bytes from start to EOF
  - suffix range         -> 206 + last N bytes
  - invalid range        -> 416 with Content-Range: bytes */<size>
  - no Range header      -> 200 full body with Accept-Ranges: bytes

action=download keeps returning FileResponse (Starlette serves Range for it
natively at the ASGI layer), so the no-range download contract is unchanged.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.responses import FileResponse, StreamingResponse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.downloads import get_download_file, _resolve_byte_range
from auth import AuthContext
from models import Download, DownloadStatus

CONTENT = b"abcdefghijklmnopqrstuvwxyz"  # 26 bytes
FILE_SIZE = len(CONTENT)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _make_download(output_path):
    dl = Download()
    dl.id = 1
    dl.status = DownloadStatus.COMPLETED.value
    dl.output_path = output_path
    dl.requested_by_user_id = None
    return dl


def _make_session(download):
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _ScalarResult(download),
        _ScalarResult(None),  # AppSettings row
    ])
    return session


def _request_with_range(range_value):
    headers = {"range": range_value} if range_value is not None else {}
    return SimpleNamespace(headers=headers)


async def _read_streaming_body(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


class ServeFileRangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.folder = self._tmpdir.name
        self.file_path = str(Path(self.folder) / "Show.ts")
        Path(self.file_path).write_bytes(CONTENT)
        self.auth = AuthContext(authenticated=True, is_admin=True, user_id=1)

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def _serve(self, range_value, action="play"):
        session = _make_session(_make_download(self.file_path))
        with patch("api.downloads.settings") as mock_cfg:
            mock_cfg.default_download_folder = self.folder
            mock_cfg.default_completed_folder = self.folder
            return await get_download_file(
                1, _request_with_range(range_value), action, self.auth, session
            )

    async def test_valid_bounded_range_returns_206_with_exact_bytes(self):
        response = await self._serve("bytes=2-5")
        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], f"bytes 2-5/{FILE_SIZE}")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        body = await _read_streaming_body(response)
        self.assertEqual(body, CONTENT[2:6], "Body must contain exactly the requested bytes.")

    async def test_open_ended_range_returns_tail_from_start(self):
        response = await self._serve("bytes=10-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(
            response.headers["content-range"], f"bytes 10-{FILE_SIZE - 1}/{FILE_SIZE}"
        )
        body = await _read_streaming_body(response)
        self.assertEqual(body, CONTENT[10:], "Open-ended range must stream to EOF.")

    async def test_suffix_range_returns_last_bytes(self):
        response = await self._serve("bytes=-5")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(
            response.headers["content-range"],
            f"bytes {FILE_SIZE - 5}-{FILE_SIZE - 1}/{FILE_SIZE}",
        )
        body = await _read_streaming_body(response)
        self.assertEqual(body, CONTENT[-5:])

    async def test_range_end_clamped_to_file_size(self):
        """An end beyond EOF is clamped per RFC 7233 instead of erroring."""
        response = await self._serve("bytes=20-999")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(
            response.headers["content-range"], f"bytes 20-{FILE_SIZE - 1}/{FILE_SIZE}"
        )
        body = await _read_streaming_body(response)
        self.assertEqual(body, CONTENT[20:])

    async def test_range_start_past_eof_returns_416_with_content_range(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._serve(f"bytes={FILE_SIZE}-")
        self.assertEqual(ctx.exception.status_code, 416)
        self.assertEqual(
            ctx.exception.headers.get("Content-Range"),
            f"bytes */{FILE_SIZE}",
            "416 must carry Content-Range: bytes */<size> so the player can recover.",
        )

    async def test_malformed_range_returns_416(self):
        for bad in ("bytes=abc-def", "items=0-5", "bytes=5-3", "bytes=-0"):
            with self.subTest(range_header=bad):
                with self.assertRaises(HTTPException) as ctx:
                    await self._serve(bad)
                self.assertEqual(ctx.exception.status_code, 416)

    async def test_multiple_ranges_rejected_with_416(self):
        """Only single ranges are supported; multipart ranges are rejected."""
        with self.assertRaises(HTTPException) as ctx:
            await self._serve("bytes=0-1,3-4")
        self.assertEqual(ctx.exception.status_code, 416)

    async def test_no_range_header_returns_200_full_content(self):
        response = await self._serve(None)
        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-length"], str(FILE_SIZE))
        self.assertNotIn("content-range", response.headers)
        body = await _read_streaming_body(response)
        self.assertEqual(body, CONTENT, "No-range request must stream the whole file.")

    async def test_download_action_still_returns_file_response(self):
        """action=download stays a FileResponse (Starlette handles Range for it)."""
        response = await self._serve("bytes=0-3", action="download")
        self.assertIsInstance(response, FileResponse)


class ResolveByteRangeUnitTests(unittest.TestCase):
    def test_bounded_range(self):
        self.assertEqual(_resolve_byte_range("bytes=2-5", 26), (2, 5))

    def test_open_ended_range(self):
        self.assertEqual(_resolve_byte_range("bytes=10-", 26), (10, 25))

    def test_suffix_range(self):
        self.assertEqual(_resolve_byte_range("bytes=-5", 26), (21, 25))

    def test_suffix_longer_than_file_returns_whole_file(self):
        self.assertEqual(_resolve_byte_range("bytes=-999", 26), (0, 25))

    def test_end_clamped(self):
        self.assertEqual(_resolve_byte_range("bytes=20-999", 26), (20, 25))

    def test_start_past_eof_raises(self):
        with self.assertRaises(ValueError):
            _resolve_byte_range("bytes=26-", 26)

    def test_inverted_range_raises(self):
        with self.assertRaises(ValueError):
            _resolve_byte_range("bytes=5-3", 26)

    def test_zero_suffix_raises(self):
        with self.assertRaises(ValueError):
            _resolve_byte_range("bytes=-0", 26)


if __name__ == "__main__":
    unittest.main()
