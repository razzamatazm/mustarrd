"""The .nfo sidecar lands beside the final completed recording.

Covers the pipeline end of issue #432: metadata captured when the recording is
queued, the sidecar written after Comskip and transcoding have moved the file
to its final name, and the guarantee that a sidecar failure never costs the
user the recording.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from database import Base, _apply_lightweight_migrations
from models import DownloadStatus
from services import nfo_writer
from services.download_builder import capture_guide_metadata
from services.download_manager import DownloadManager


PROGRAM = {
    "title": "Detectorists",
    "description": "Two men sweep a field.",
    "season": 2,
    "episode": 4,
    "subtitle": "The Hoard",
    "categories": ["Comedy", "Drama"],
    "tvdb_id": "281566",
    "imdb_id": "tt3630622",
}


def _make_download(output_path, *, is_vod=False):
    download = MagicMock()
    download.id = 7
    download.is_vod = is_vod
    download.status = DownloadStatus.PROCESSING.value
    download.output_path = output_path
    download.program_title = PROGRAM["title"]
    download.program_start = datetime(2026, 2, 1, 21, 0)
    download.duration_minutes = 30
    download.recorded_duration_seconds = None
    download.guide_metadata_json = capture_guide_metadata(PROGRAM)
    download.progress = 0.0
    download.completed_at = None
    download.error_message = None
    return download


def _make_settings(**overrides):
    settings = MagicMock()
    settings.write_nfo_files = True
    settings.integrity_check_enabled = False
    settings.delete_original_after_transcode = True
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class SidecarPipelineTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.download_folder = self.root / "downloads"
        self.completed_folder = self.root / "completed"
        self.download_folder.mkdir()
        self.completed_folder.mkdir()

    def _session(self, download, settings):
        mock_session = AsyncMock()
        # The row lookup comes first, the settings lookup second; everything
        # after that (schedule sync, bookkeeping) gets an empty result.
        queued = [download, settings]

        async def execute(_statement):
            result = MagicMock()
            result.scalar_one_or_none.return_value = queued.pop(0) if queued else None
            result.scalars.return_value.all.return_value = []
            return result

        mock_session.execute = execute
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def ctx():
            yield mock_session

        return ctx

    def _run_post_process(self, download, settings, *, transcode=True):
        """Drive _execute_post_process with a realistic Comskip + transcode move."""
        source = self.download_folder / "Detectorists - S02E04.ts"
        source.write_bytes(b"\x00" * 512)
        download.output_path = str(source)

        # Comskip + FFmpeg leave a differently-named file behind; the pipeline
        # then moves that file, not the .ts, into the completed folder.
        transcoded = self.download_folder / "Detectorists - S02E04.mkv"

        async def fake_post_process(path, download_id, session, settings_arg):
            transcoded.write_bytes(b"\x00" * 400)
            return str(transcoded), []

        async def fake_move(path, completed_folder, download_folder):
            target = Path(completed_folder) / Path(path).name
            shutil.move(path, target)
            return str(target)

        patches = [
            patch("services.download_manager.async_session_maker", self._session(download, settings)),
            patch.object(self.manager, "_needs_post_processing", return_value=transcode),
            patch.object(self.manager, "_post_process", fake_post_process),
            patch.object(self.manager, "_move_to_completed_async", fake_move),
            patch.object(self.manager, "_resolve_completed_folder", return_value=str(self.completed_folder)),
            patch.object(self.manager, "_resolve_download_folder", return_value=str(self.download_folder)),
            patch.object(self.manager, "_store_recorded_duration", AsyncMock()),
            patch.object(self.manager, "_broadcast_progress", AsyncMock()),
            patch.object(self.manager, "_broadcast_log", AsyncMock()),
            patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        asyncio.run(self.manager._execute_post_process(download.id))

    def test_sidecar_sits_beside_the_final_transcoded_file(self):
        download = _make_download(None)
        self._run_post_process(download, _make_settings())

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        sidecar = self.completed_folder / "Detectorists - S02E04.nfo"
        self.assertTrue(sidecar.is_file(), sorted(os.listdir(self.completed_folder)))
        # Never beside the intermediate .ts in the download folder.
        self.assertFalse((self.download_folder / "Detectorists - S02E04.nfo").exists())

        root = ET.fromstring(sidecar.read_text())
        self.assertEqual(root.tag, "episodedetails")
        self.assertEqual(root.findtext("showtitle"), "Detectorists")
        self.assertEqual(root.findtext("title"), "The Hoard")
        self.assertEqual(root.findtext("season"), "2")
        self.assertEqual(root.findtext("episode"), "4")
        self.assertEqual(root.findtext("plot"), "Two men sweep a field.")
        self.assertEqual(root.findtext("aired"), "2026-02-01")
        self.assertEqual(root.findtext("runtime"), "30")
        self.assertEqual(
            [(el.get("type"), el.get("default")) for el in root.findall("uniqueid")],
            [("tvdb", "true"), ("imdb", None)],
        )

    def test_setting_off_writes_nothing_and_still_completes(self):
        download = _make_download(None)
        self._run_post_process(download, _make_settings(write_nfo_files=False))

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertEqual(
            [p for p in os.listdir(self.completed_folder) if p.endswith(".nfo")], []
        )

    def test_vod_download_gets_no_sidecar(self):
        download = _make_download(None, is_vod=True)
        self._run_post_process(download, _make_settings())

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertEqual(
            [p for p in os.listdir(self.completed_folder) if p.endswith(".nfo")], []
        )

    def test_unwritable_completed_folder_still_completes_the_recording(self):
        download = _make_download(None)
        original_write = nfo_writer.write_sidecar

        def deny(video_path, details):
            os.chmod(self.completed_folder, 0o500)
            self.addCleanup(os.chmod, self.completed_folder, 0o700)
            try:
                return original_write(video_path, details)
            finally:
                os.chmod(self.completed_folder, 0o700)

        with patch.object(nfo_writer, "write_sidecar", deny):
            self._run_post_process(download, _make_settings())

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.error_message)
        self.assertFalse((self.completed_folder / "Detectorists - S02E04.nfo").exists())

    def test_a_raised_sidecar_error_never_fails_the_recording(self):
        download = _make_download(None)
        with patch.object(nfo_writer, "write_sidecar", side_effect=OSError("boom")):
            self._run_post_process(download, _make_settings())

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)


def _recovery_session(downloads, settings):
    settings_result = MagicMock()
    settings_result.scalar_one_or_none.return_value = settings
    downloads_result = MagicMock()
    downloads_result.scalars.return_value.all.return_value = downloads
    calls = [0]

    async def execute(_statement):
        calls[0] += 1
        return settings_result if calls[0] == 1 else downloads_result

    session = AsyncMock()
    session.execute = execute
    session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield session

    return ctx


class RestartRecoverySidecarTests(unittest.IsolatedAsyncioTestCase):
    """A recording finalized by restart recovery gets its sidecar too.

    recover_incomplete_downloads finishes downloads that were in flight when
    the app was killed. Those never pass through _execute_post_process, so the
    sidecar has to be written here as well or a restart silently costs the
    user the .nfo.
    """

    async def asyncSetUp(self):
        self.manager = DownloadManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.download_folder = root / "downloads"
        self.completed_folder = root / "completed"
        self.download_folder.mkdir()
        self.completed_folder.mkdir()

    async def test_recovered_download_gets_a_sidecar(self):
        source = self.download_folder / "Detectorists - S02E04.ts"
        source.write_bytes(b"\x00" * 256)

        download = _make_download(str(source))
        download.status = DownloadStatus.DOWNLOADING.value
        download.file_size = 256
        download.downloaded_bytes = 256

        async def fake_move(path, completed_folder, download_folder):
            target = Path(completed_folder) / Path(path).name
            shutil.move(path, target)
            return str(target)

        with (
            patch(
                "services.download_manager.async_session_maker",
                _recovery_session([download], _make_settings()),
            ),
            patch.object(self.manager, "_needs_post_processing", return_value=False),
            patch.object(self.manager, "_move_to_completed_async", fake_move),
            patch.object(self.manager, "_resolve_completed_folder", return_value=str(self.completed_folder)),
            patch.object(self.manager, "_resolve_download_folder", return_value=str(self.download_folder)),
            patch.object(self.manager, "_broadcast_log", AsyncMock()),
            patch.object(self.manager, "_broadcast_progress", AsyncMock()),
        ):
            await self.manager.recover_incomplete_downloads()

        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        sidecar = self.completed_folder / "Detectorists - S02E04.nfo"
        self.assertTrue(sidecar.is_file(), sorted(os.listdir(self.completed_folder)))
        self.assertEqual(ET.fromstring(sidecar.read_text()).tag, "episodedetails")


class QueueTimeCaptureTests(unittest.TestCase):
    def test_captured_metadata_is_what_lands_in_the_nfo(self):
        """The sidecar reads the snapshot, never the live guide row.

        epg_programs rows are overwritten and pruned by guide ingest, so a
        recording queued today can have no matching row by the time
        post-processing runs.
        """
        captured = capture_guide_metadata(PROGRAM)
        download = _make_download("/completed/show.mkv")
        download.guide_metadata_json = captured

        # The guide moves on: the row this program came from now says
        # something else entirely. Nothing re-reads it.
        stale_program = dict(PROGRAM, title="Infomercial", season=None, episode=None)
        capture_guide_metadata(stale_program)

        root = ET.fromstring(nfo_writer.render_nfo(nfo_writer.details_from_download(download)))
        self.assertEqual(root.tag, "episodedetails")
        self.assertEqual(root.findtext("showtitle"), "Detectorists")
        self.assertEqual(root.findtext("season"), "2")

    def test_program_with_no_metadata_captures_nothing(self):
        self.assertIsNone(capture_guide_metadata({"title": "Just A Title"}))

    def test_capture_is_json_the_writer_can_read_back(self):
        payload = json.loads(capture_guide_metadata(PROGRAM))
        self.assertEqual(payload["description"], "Two men sweep a field.")
        self.assertEqual(payload["categories"], ["Comedy", "Drama"])
        self.assertEqual(payload["tvdb_id"], "281566")

    def test_unreadable_snapshot_degrades_to_a_bare_nfo(self):
        download = _make_download("/completed/show.mkv")
        download.guide_metadata_json = "{not json"

        root = ET.fromstring(nfo_writer.render_nfo(nfo_writer.details_from_download(download)))
        self.assertEqual(root.tag, "movie")
        self.assertEqual(root.findtext("title"), "Detectorists")

    def test_probed_duration_wins_over_the_guide_duration(self):
        download = _make_download("/completed/show.mkv")
        download.recorded_duration_seconds = 1500
        details = nfo_writer.details_from_download(download)
        self.assertEqual(details.runtime_minutes, 25)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """A database created before this change gains the new columns on startup."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Roll the schema back to how it looked before this feature.
            await conn.execute(text("ALTER TABLE downloads DROP COLUMN guide_metadata_json"))
            await conn.execute(text("ALTER TABLE app_settings DROP COLUMN write_nfo_files"))

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _columns(self, conn, table):
        def _read(sync_conn):
            return {col["name"] for col in inspect(sync_conn).get_columns(table)}

        return await conn.run_sync(_read)

    async def test_startup_adds_the_new_columns(self):
        async with self.engine.begin() as conn:
            self.assertNotIn("guide_metadata_json", await self._columns(conn, "downloads"))
            await _apply_lightweight_migrations(conn)
            self.assertIn("guide_metadata_json", await self._columns(conn, "downloads"))
            self.assertIn("write_nfo_files", await self._columns(conn, "app_settings"))

    async def test_migration_is_idempotent(self):
        async with self.engine.begin() as conn:
            await _apply_lightweight_migrations(conn)
            await _apply_lightweight_migrations(conn)
            self.assertIn("guide_metadata_json", await self._columns(conn, "downloads"))


if __name__ == "__main__":
    unittest.main()
