"""
Regression tests: completed downloads persist the real recording duration.

Feature: completed recording cards display the actual file duration. The EPG
program duration (duration_minutes) can differ from the recorded file (padding,
provider drift, partial transfers), so on completion the file is probed with
ffprobe (post_processor._get_duration) and the result is stored on the row as
recorded_duration_seconds via a lightweight ALTER TABLE migration.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from database import _apply_lightweight_migrations, _column_exists
from services.download_manager import DownloadManager
from models import Download, DownloadStatus


def _make_session_ctx(download):
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = download
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield mock_session

    return ctx


class RecordedDurationDownloadPathTests(unittest.TestCase):
    """_execute_download (no post-processing) persists recorded_duration_seconds."""

    def setUp(self):
        self.manager = DownloadManager()

    def _run_download(self, get_duration_mock):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")

            mock_download = MagicMock()
            mock_download.id = 1
            mock_download.status = DownloadStatus.PENDING.value
            mock_download.output_path = output_file
            mock_download.source_url = "http://provider/stream"
            mock_download.progress = 0.0
            mock_download.downloaded_bytes = 0
            mock_download.file_size = 0
            mock_download.error_message = None
            mock_download.recorded_duration_seconds = None

            async def fake_download(url, path, dl_id, ses, offset=0):
                Path(path).write_bytes(b"\x47" * 1024)
                return 1024

            with (
                patch("services.download_manager.async_session_maker", _make_session_ctx(mock_download)),
                patch.object(self.manager, "_download_file", fake_download),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_needs_post_processing", return_value=False),
                patch.object(self.manager, "_move_to_completed", side_effect=lambda p, c, d: p),
                patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
                patch(
                    "services.post_processor.post_processor._get_duration",
                    get_duration_mock,
                ),
            ):
                asyncio.run(self.manager._execute_download(1))

            return mock_download

    def test_completed_download_persists_recorded_duration(self):
        """A 1h02m03s file (3723.4s) must be stored as 3723 seconds."""
        download = self._run_download(AsyncMock(return_value=3723.4))
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertEqual(
            download.recorded_duration_seconds,
            3723,
            "Completed download must persist the ffprobe duration (rounded to seconds).",
        )

    def test_zero_duration_probe_leaves_column_null(self):
        """ffprobe returning 0 (probe unavailable/failed) must not store a bogus value."""
        download = self._run_download(AsyncMock(return_value=0))
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.recorded_duration_seconds)

    def test_probe_exception_does_not_break_completion(self):
        """An ffprobe crash must never fail an otherwise complete recording."""
        download = self._run_download(AsyncMock(side_effect=RuntimeError("ffprobe exploded")))
        self.assertEqual(
            download.status,
            DownloadStatus.COMPLETED.value,
            "Duration probing is best-effort; a probe failure must not fail the download.",
        )
        self.assertIsNone(download.recorded_duration_seconds)


class RecordedDurationPostProcessPathTests(unittest.TestCase):
    """_execute_post_process completion paths persist recorded_duration_seconds."""

    def setUp(self):
        self.manager = DownloadManager()

    def test_post_process_completion_persists_recorded_duration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "show.ts")
            Path(input_path).write_bytes(b"\x47" * 1024)

            mock_download = MagicMock()
            mock_download.id = 2
            mock_download.status = DownloadStatus.PROCESSING.value
            mock_download.output_path = input_path
            mock_download.progress = 0.0
            mock_download.error_message = None
            mock_download.recorded_duration_seconds = None

            async def fake_post_process(path, dl_id, ses, settings):
                return path, []

            with (
                patch("services.download_manager.async_session_maker", _make_session_ctx(mock_download)),
                patch.object(self.manager, "_needs_post_processing", return_value=True),
                patch.object(self.manager, "_post_process", fake_post_process),
                patch.object(self.manager, "_move_to_completed", side_effect=lambda p, c, d: p),
                patch.object(self.manager, "_cleanup_working_files", MagicMock()),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
                patch(
                    "services.post_processor.post_processor._get_duration",
                    AsyncMock(return_value=1845.7),
                ),
            ):
                asyncio.run(self.manager._execute_post_process(2))

            self.assertEqual(mock_download.status, DownloadStatus.COMPLETED.value)
            self.assertEqual(
                mock_download.recorded_duration_seconds,
                1846,
                "Post-processed download must persist the probed duration of the final file.",
            )


class RecordedDurationSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Migration and serialization coverage for the new column."""

    async def test_lightweight_migration_adds_recorded_duration_column(self):
        """Existing installs (downloads table without the column) get it via ALTER TABLE."""
        engine = create_async_engine("sqlite+aiosqlite://")
        legacy_tables = [
            "plex_servers",
            "app_settings",
            "epg_programs",
            "xtream_accounts",
            "scheduled_recordings",
            "downloads",
        ]
        try:
            async with engine.begin() as conn:
                for table in legacy_tables:
                    await conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                await _apply_lightweight_migrations(conn)
                self.assertTrue(
                    await _column_exists(conn, "downloads", "recorded_duration_seconds"),
                    "Startup migration must add downloads.recorded_duration_seconds.",
                )
        finally:
            await engine.dispose()

    async def test_to_dict_exposes_recorded_duration_seconds(self):
        download = Download()
        download.recorded_duration_seconds = 3723
        payload = download.to_dict()
        self.assertEqual(payload["recorded_duration_seconds"], 3723)


if __name__ == "__main__":
    unittest.main()
