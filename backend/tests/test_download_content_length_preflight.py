"""
Regression: no disk-space preflight compared the provider's Content-Length to
the free space on the download folder before streaming began.

The only space checks were the queue-time min-free check (services/disk_space)
and the recovery-time min-free check; neither knows the size of the incoming
file. A 30 GB recording onto a disk with 5 GB free streamed until ENOSPC,
wasting hours and a provider connection slot.

Fix: _stream_response_to_file calls _preflight_disk_space before opening the
output file. When Content-Length is known, the remaining bytes plus the
configured minimum free space (AppSettings.min_free_space_gb, default 25 GB)
must fit in the free space of the download folder, otherwise the download
fails immediately with a clear error.
"""

import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings
from services.download_manager import DownloadManager

DiskUsage = namedtuple("usage", ["total", "used", "free"])

GIB = 1024 ** 3


def _make_response(chunks):
    response = MagicMock()
    response.status = 200
    response.reason = "OK"
    response.headers = {}

    async def iter_chunked(_size):
        for chunk in chunks:
            yield chunk

    response.content.iter_chunked = iter_chunked
    return response


def _make_db_session(min_free_space_gb):
    settings_row = AppSettings(min_free_space_gb=min_free_space_gb)

    result = MagicMock()
    result.scalar_one_or_none.return_value = settings_row

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


class ContentLengthPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_length_exceeding_free_space_fails_before_streaming(self):
        """
        Content-Length 30 GiB, 5 GiB free, 2 GiB min-free: must raise a clear
        disk-space error before writing a single byte.
        """
        manager = DownloadManager()
        db_session = _make_db_session(min_free_space_gb=2)
        response = _make_response([b"\x47" * 1024])

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch(
                    "services.download_manager.shutil.disk_usage",
                    return_value=DiskUsage(100 * GIB, 95 * GIB, 5 * GIB),
                ),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                with self.assertRaises(Exception) as ctx:
                    await manager._stream_response_to_file(
                        response=response,
                        output_path=tmp_path,
                        file_mode="wb",
                        downloaded_start=0,
                        total_size=30 * GIB,
                        download_id=7,
                        session=db_session,
                    )

            self.assertIn("disk space", str(ctx.exception).lower())
            self.assertEqual(
                os.path.getsize(tmp_path), 0,
                "No bytes must be written when the preflight fails.",
            )
        finally:
            os.unlink(tmp_path)

    async def test_min_free_space_setting_is_respected(self):
        """
        Content-Length 3 GiB, 10 GiB free: fits on its own, but with a 9 GiB
        min-free setting the download would push free space below the floor,
        so it must fail.
        """
        manager = DownloadManager()
        db_session = _make_db_session(min_free_space_gb=9)
        response = _make_response([b"\x47" * 1024])

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch(
                    "services.download_manager.shutil.disk_usage",
                    return_value=DiskUsage(100 * GIB, 90 * GIB, 10 * GIB),
                ),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                with self.assertRaises(Exception) as ctx:
                    await manager._stream_response_to_file(
                        response=response,
                        output_path=tmp_path,
                        file_mode="wb",
                        downloaded_start=0,
                        total_size=3 * GIB,
                        download_id=8,
                        session=db_session,
                    )
            self.assertIn("disk space", str(ctx.exception).lower())
        finally:
            os.unlink(tmp_path)

    async def test_fitting_download_streams_normally(self):
        """Content-Length that fits (plus min-free) must stream as before."""
        manager = DownloadManager()
        db_session = _make_db_session(min_free_space_gb=2)
        body = b"\x47" * 1024
        response = _make_response([body])

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch(
                    "services.download_manager.shutil.disk_usage",
                    return_value=DiskUsage(100 * GIB, 50 * GIB, 50 * GIB),
                ),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._stream_response_to_file(
                    response=response,
                    output_path=tmp_path,
                    file_mode="wb",
                    downloaded_start=0,
                    total_size=1024,
                    download_id=9,
                    session=db_session,
                )
            self.assertEqual(total, 1024)
            with open(tmp_path, "rb") as fh:
                self.assertEqual(fh.read(), body)
        finally:
            os.unlink(tmp_path)

    async def test_resume_only_counts_remaining_bytes(self):
        """
        Resuming at byte 6 GiB of an 8 GiB file with 3 GiB free and 0 min-free:
        only the remaining 2 GiB must be required, so the resume proceeds.
        """
        manager = DownloadManager()
        db_session = _make_db_session(min_free_space_gb=0)
        body = b"\x47" * 512
        response = _make_response([body])

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch(
                    "services.download_manager.shutil.disk_usage",
                    return_value=DiskUsage(100 * GIB, 97 * GIB, 3 * GIB),
                ),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._stream_response_to_file(
                    response=response,
                    output_path=tmp_path,
                    file_mode="ab",
                    downloaded_start=6 * GIB,
                    total_size=8 * GIB,
                    download_id=10,
                    session=db_session,
                )
            self.assertEqual(total, 6 * GIB + 512)
        finally:
            os.unlink(tmp_path)

    async def test_unknown_length_skips_preflight(self):
        """Chunked transfer (no Content-Length) must not query disk usage."""
        manager = DownloadManager()
        db_session = _make_db_session(min_free_space_gb=2)
        body = b"\x47" * 256
        response = _make_response([body])

        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            tmp_path = f.name

        try:
            with (
                patch(
                    "services.download_manager.shutil.disk_usage",
                    side_effect=AssertionError("disk_usage must not be called for unknown length"),
                ),
                patch.object(manager, "_broadcast_log", AsyncMock()),
                patch.object(manager, "_broadcast_progress", AsyncMock()),
            ):
                total = await manager._stream_response_to_file(
                    response=response,
                    output_path=tmp_path,
                    file_mode="wb",
                    downloaded_start=0,
                    total_size=0,
                    download_id=11,
                    session=db_session,
                )
            self.assertEqual(total, 256)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()
