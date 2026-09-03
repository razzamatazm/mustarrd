"""Regression tests for the INTERRUPTED download status (issue #435).

A capture that stops early but leaves a file ffprobe can open is kept and
marked INTERRUPTED. Anything ffprobe cannot open (including a zero-byte file)
keeps the historical behaviour: FAILED, and the partial is deleted.
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

from services.download_manager import DownloadManager
from models import DownloadStatus
from models.scheduled_recording import ScheduledStatus


def _make_download(download_id, output_path, **overrides):
    dl = MagicMock()
    dl.id = download_id
    dl.status = DownloadStatus.PENDING.value
    dl.output_path = output_path
    dl.source_url = "http://provider/timeshift/user/pass/60/2026-06-06:00-00/1.ts"
    dl.progress = 0.0
    dl.downloaded_bytes = 0
    dl.duration_minutes = 60
    dl.recorded_duration_seconds = None
    dl.interruption_reason = None
    dl.is_vod = False
    dl.error_message = None
    dl.completed_at = None
    for key, value in overrides.items():
        setattr(dl, key, value)
    return dl


def _session_ctx(download, settings_row=None):
    """Session whose first execute() yields the download, later ones settings."""
    download_result = MagicMock()
    download_result.scalar_one_or_none.return_value = download
    settings_result = MagicMock()
    settings_result.scalar_one_or_none.return_value = settings_row

    session = AsyncMock()
    session.commit = AsyncMock()
    calls = {"n": 0}

    async def execute(stmt):
        calls["n"] += 1
        # The download row is looked up first, and again in the error handler.
        text = str(stmt).lower()
        if "downloads" in text and "app_settings" not in text:
            return download_result
        if "scheduled_recordings" in text:
            return settings_result
        return settings_result

    session.execute = execute

    @asynccontextmanager
    async def maker():
        yield session

    return maker, session


def _probe(ok: bool, checked: bool = True):
    async def probe(path, log_callback=None):
        return {"checked": checked, "ok": ok, "reason": None if ok else "the container could not be parsed"}
    return probe


class InterruptedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def _run_interrupted_download(self, tmpdir, probe_result, needs_post=False):
        output_file = os.path.join(tmpdir, "show.ts")
        download = _make_download(1, output_file)
        maker, _session = _session_ctx(download)

        async def fake_download_file(url, path, download_id, session, offset=0):
            with open(path, "wb") as f:
                f.write(b"\x00" * 2048)
            raise Exception("Connection to the provider was lost.")

        with patch("services.download_manager.async_session_maker", maker), \
                patch.object(self.manager, "_download_file", fake_download_file), \
                patch.object(self.manager, "_broadcast_progress", AsyncMock()), \
                patch.object(self.manager, "_broadcast_log", AsyncMock()), \
                patch.object(self.manager, "_needs_post_processing", return_value=needs_post), \
                patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=None)), \
                patch.object(self.manager, "_store_recorded_duration", AsyncMock()), \
                patch.object(self.manager, "_move_to_completed", return_value=output_file), \
                patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir), \
                patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir), \
                patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()), \
                patch("services.post_processor.post_processor.probe_media_integrity", probe_result):
            asyncio.run(self.manager._execute_download(1))

        return download, output_file

    def test_playable_partial_is_kept_and_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            download, output_file = self._run_interrupted_download(tmpdir, _probe(True))

            self.assertEqual(download.status, DownloadStatus.INTERRUPTED.value)
            self.assertTrue(
                os.path.exists(output_file),
                "A playable partial recording must survive the failure handler",
            )
            self.assertIn("lost", (download.interruption_reason or "").lower())

    def test_unplayable_partial_still_fails_and_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            download, output_file = self._run_interrupted_download(tmpdir, _probe(False))

            self.assertEqual(download.status, DownloadStatus.FAILED.value)
            self.assertFalse(
                os.path.exists(output_file),
                "An unplayable partial must keep today's delete-and-fail behaviour",
            )
            self.assertIsNone(download.interruption_reason)

    def test_ffprobe_unavailable_keeps_delete_and_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            download, output_file = self._run_interrupted_download(
                tmpdir, _probe(False, checked=False)
            )

            self.assertEqual(download.status, DownloadStatus.FAILED.value)
            self.assertFalse(os.path.exists(output_file))

    def test_zero_byte_partial_still_fails_and_is_deleted(self):
        """The empty-response path must not become an interrupted recording."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "empty.ts")
            download = _make_download(2, output_file)
            maker, _session = _session_ctx(download)

            async def fake_download_file(url, path, download_id, session, offset=0):
                open(path, "wb").close()
                return 0

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_download_file", fake_download_file), \
                    patch.object(self.manager, "_broadcast_progress", AsyncMock()), \
                    patch.object(self.manager, "_broadcast_log", AsyncMock()), \
                    patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=None)), \
                    patch("services.post_processor.post_processor.probe_media_integrity", _probe(True)):
                asyncio.run(self.manager._execute_download(2))

            self.assertEqual(download.status, DownloadStatus.FAILED.value)
            self.assertFalse(os.path.exists(output_file))

    def test_interrupted_recording_is_queued_for_post_processing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            download, output_file = self._run_interrupted_download(
                tmpdir, _probe(True), needs_post=True
            )

            self.assertEqual(download.status, DownloadStatus.PROCESSING.value)
            self.assertIsNotNone(download.interruption_reason)
            self.assertTrue(os.path.exists(output_file))
            self.assertFalse(self.manager._post_queue.empty())

    def test_interruption_and_integrity_warning_coexist(self):
        """An interrupted recording that also probes oddly surfaces both facts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, "show.ts")
            download = _make_download(3, output_file)
            maker, _session = _session_ctx(download)

            async def fake_download_file(url, path, download_id, session, offset=0):
                with open(path, "wb") as f:
                    f.write(b"\x00" * 2048)
                raise Exception("Connection to the provider was lost.")

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_download_file", fake_download_file), \
                    patch.object(self.manager, "_broadcast_progress", AsyncMock()), \
                    patch.object(self.manager, "_broadcast_log", AsyncMock()), \
                    patch.object(self.manager, "_needs_post_processing", return_value=False), \
                    patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_store_recorded_duration", AsyncMock()), \
                    patch.object(self.manager, "_partial_is_playable", AsyncMock(return_value=True)), \
                    patch.object(self.manager, "_integrity_check_warning",
                                 AsyncMock(return_value="file may be corrupt (the file reports zero duration)")), \
                    patch.object(self.manager, "_move_to_completed", return_value=output_file), \
                    patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()):
                asyncio.run(self.manager._execute_download(3))

            self.assertEqual(download.status, DownloadStatus.INTERRUPTED.value)
            self.assertIn("lost", (download.interruption_reason or "").lower())
            self.assertIn("Completed with warnings", download.error_message or "")

    def test_post_processing_preserves_interrupted_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "show.ts")
            with open(source, "wb") as f:
                f.write(b"\x00" * 2048)
            final = os.path.join(tmpdir, "show.mkv")
            with open(final, "wb") as f:
                f.write(b"\x00" * 1024)

            download = _make_download(
                4, source,
                status=DownloadStatus.PROCESSING.value,
                interruption_reason="Connection to the provider was lost.",
            )
            maker, _session = _session_ctx(download)

            async def fake_post_process(path, download_id, session, settings):
                return final, []

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_broadcast_progress", AsyncMock()), \
                    patch.object(self.manager, "_broadcast_log", AsyncMock()), \
                    patch.object(self.manager, "_needs_post_processing", return_value=True), \
                    patch.object(self.manager, "_post_process", fake_post_process), \
                    patch.object(self.manager, "_store_recorded_duration", AsyncMock()), \
                    patch.object(self.manager, "_integrity_check_warning", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_move_to_completed", return_value=final), \
                    patch.object(self.manager, "_move_sidecar_to_completed", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_cleanup_working_files", MagicMock()), \
                    patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()):
                asyncio.run(self.manager._execute_post_process(4))

            self.assertEqual(
                download.status,
                DownloadStatus.INTERRUPTED.value,
                "Reaching the completed folder must not promote an interrupted recording",
            )

    def test_post_processing_still_completes_a_normal_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = os.path.join(tmpdir, "show.ts")
            with open(source, "wb") as f:
                f.write(b"\x00" * 2048)

            download = _make_download(5, source, status=DownloadStatus.PROCESSING.value)
            maker, _session = _session_ctx(download)

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_broadcast_progress", AsyncMock()), \
                    patch.object(self.manager, "_broadcast_log", AsyncMock()), \
                    patch.object(self.manager, "_needs_post_processing", return_value=False), \
                    patch.object(self.manager, "_store_recorded_duration", AsyncMock()), \
                    patch.object(self.manager, "_integrity_check_warning", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_move_to_completed", return_value=source), \
                    patch.object(self.manager, "_resolve_completed_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_resolve_download_folder", return_value=tmpdir), \
                    patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()):
                asyncio.run(self.manager._execute_post_process(5))

            self.assertEqual(download.status, DownloadStatus.COMPLETED.value)


class InterruptedRetryTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_retry_recaptures_to_the_download_folder(self):
        """Retrying must not overwrite the kept partial in the completed folder."""
        with tempfile.TemporaryDirectory() as root:
            completed_folder = os.path.join(root, "completed")
            download_folder = os.path.join(root, "downloads")
            os.makedirs(completed_folder)
            os.makedirs(download_folder)
            kept = os.path.join(completed_folder, "Show", "show.mkv")
            os.makedirs(os.path.dirname(kept))
            with open(kept, "wb") as f:
                f.write(b"\x00" * 16)

            download = _make_download(
                6, kept,
                status=DownloadStatus.INTERRUPTED.value,
                interruption_reason="Connection to the provider was lost.",
                recorded_duration_seconds=3240,
                progress=90.0,
                downloaded_bytes=999,
            )
            maker, _session = _session_ctx(download)

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_resolve_completed_folder", return_value=completed_folder), \
                    patch.object(self.manager, "_resolve_download_folder", return_value=download_folder), \
                    patch.object(self.manager, "_sync_schedule_status", AsyncMock()):
                result = asyncio.run(self.manager.retry_download(6))

            self.assertTrue(result, "An interrupted recording must be retryable")
            self.assertEqual(download.status, DownloadStatus.PENDING.value)
            self.assertIsNone(download.interruption_reason)
            self.assertIsNone(download.recorded_duration_seconds)
            self.assertEqual(download.downloaded_bytes, 0)
            self.assertEqual(
                download.output_path,
                os.path.join(download_folder, "Show", "show.ts"),
                "Retry must re-capture a fresh .ts into the download folder, not over "
                "the post-processed file it kept",
            )
            self.assertTrue(os.path.exists(kept), "The kept partial must survive a retry")

    def test_retry_of_interrupted_vod_keeps_the_provider_extension(self):
        with tempfile.TemporaryDirectory() as root:
            completed_folder = os.path.join(root, "completed")
            download_folder = os.path.join(root, "downloads")
            os.makedirs(completed_folder)
            os.makedirs(download_folder)
            kept = os.path.join(completed_folder, "Movie.mp4")

            download = _make_download(
                8, kept,
                status=DownloadStatus.INTERRUPTED.value,
                interruption_reason="Connection to the provider was lost.",
                is_vod=True,
            )
            maker, _session = _session_ctx(download)

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_load_app_settings", AsyncMock(return_value=None)), \
                    patch.object(self.manager, "_resolve_completed_folder", return_value=completed_folder), \
                    patch.object(self.manager, "_resolve_download_folder", return_value=download_folder), \
                    patch.object(self.manager, "_sync_schedule_status", AsyncMock()):
                asyncio.run(self.manager.retry_download(8))

            self.assertEqual(download.output_path, os.path.join(download_folder, "Movie.mp4"))

    def test_retry_of_failed_download_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "show.ts")
            download = _make_download(7, path, status=DownloadStatus.FAILED.value)
            maker, _session = _session_ctx(download)

            with patch("services.download_manager.async_session_maker", maker), \
                    patch.object(self.manager, "_sync_schedule_status", AsyncMock()):
                result = asyncio.run(self.manager.retry_download(7))

            self.assertTrue(result)
            self.assertEqual(download.status, DownloadStatus.PENDING.value)
            self.assertEqual(download.output_path, path)


class InterruptedStatusPlumbingTests(unittest.TestCase):
    def test_scheduled_status_has_an_interrupted_member(self):
        self.assertEqual(ScheduledStatus.INTERRUPTED.value, "interrupted")

    def test_history_query_selects_interrupted(self):
        """An interrupted recording must not vanish from download history."""
        manager = DownloadManager()
        captured = {}

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session = AsyncMock()

        async def execute(stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return result

        session.execute = execute

        @asynccontextmanager
        async def maker():
            yield session

        with patch("services.download_manager.async_session_maker", maker):
            asyncio.run(manager.get_history())

        self.assertIn("interrupted", captured["sql"])

    def test_broadcast_treats_interrupted_as_terminal(self):
        manager = DownloadManager()
        manager._stage_progress[9] = {"download_progress": 50.0}
        manager._download_owners[9] = 1
        asyncio.run(manager._broadcast_progress(9, 50.0, DownloadStatus.INTERRUPTED.value))
        self.assertNotIn(9, manager._stage_progress)

    def test_schedule_status_mapping_is_terminal(self):
        from api.schedules import _map_download_status

        self.assertEqual(
            _map_download_status(DownloadStatus.INTERRUPTED.value),
            ScheduledStatus.INTERRUPTED.value,
        )


if __name__ == "__main__":
    unittest.main()
