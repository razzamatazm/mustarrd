"""
Regression tests for the post-download ffprobe integrity check.

After a download completes, the finished file gets a cheap ffprobe sanity
check (container parses, at least one stream, nonzero duration). A failing
check must NOT fail or delete the recording; it surfaces as
"Completed with warnings: file may be corrupt (...)" in error_message.
The check can be disabled via AppSettings.integrity_check_enabled.
"""
import asyncio
import json
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
from services.post_processor import PostProcessor
from models import AppSettings, DownloadStatus


# ---------------------------------------------------------------------------
# probe_media_integrity unit tests
# ---------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminate_called = False
        self.kill_called = False

    async def communicate(self):
        return self._stdout, self._stderr

    def terminate(self):
        self.terminate_called = True
        self.returncode = -15

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class HangingProcess(FakeProcess):
    def __init__(self):
        super().__init__(returncode=None)

    async def communicate(self):
        await asyncio.sleep(3600)


class ProbeMediaIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.processor = PostProcessor()

    async def _probe_with(self, process):
        with (
            patch.object(self.processor, "_resolve_ffprobe_path", return_value="/usr/bin/ffprobe"),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        ):
            return await self.processor.probe_media_integrity("/tmp/show.ts")

    async def test_healthy_file_passes(self):
        payload = json.dumps({
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            "format": {"duration": "3601.250000"},
        }).encode()
        result = await self._probe_with(FakeProcess(returncode=0, stdout=payload))
        self.assertTrue(result["checked"])
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])

    async def test_unparseable_container_fails(self):
        result = await self._probe_with(FakeProcess(returncode=1, stdout=b"", stderr=b"Invalid data"))
        self.assertTrue(result["checked"])
        self.assertFalse(result["ok"])
        self.assertIn("could not be parsed", result["reason"])

    async def test_no_streams_fails(self):
        payload = json.dumps({"streams": [], "format": {"duration": "60.0"}}).encode()
        result = await self._probe_with(FakeProcess(returncode=0, stdout=payload))
        self.assertFalse(result["ok"])
        self.assertIn("streams", result["reason"])

    async def test_zero_duration_fails(self):
        payload = json.dumps({
            "streams": [{"codec_type": "video"}],
            "format": {"duration": "0.000000"},
        }).encode()
        result = await self._probe_with(FakeProcess(returncode=0, stdout=payload))
        self.assertFalse(result["ok"])
        self.assertIn("zero duration", result["reason"])

    async def test_missing_ffprobe_skips_check(self):
        """No ffprobe means no verdict: the file must NOT be flagged."""
        with patch.object(self.processor, "_resolve_ffprobe_path", return_value=None):
            result = await self.processor.probe_media_integrity("/tmp/show.ts")
        self.assertFalse(result["checked"])
        self.assertTrue(result["ok"])

    async def test_hanging_ffprobe_times_out_and_flags_file(self):
        """A hang on corrupt input must kill ffprobe and report the file as suspect."""
        self.processor.FFPROBE_TIMEOUT_SECONDS = 0.1
        proc = HangingProcess()
        result = await self._probe_with(proc)
        self.assertTrue(result["checked"])
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["reason"])
        self.assertTrue(
            proc.terminate_called or proc.kill_called,
            "The hung ffprobe process must be terminated on timeout.",
        )


# ---------------------------------------------------------------------------
# download_manager integration: warning surfaced, recording preserved
# ---------------------------------------------------------------------------

def _make_session_ctx(download, settings_row):
    download_result = MagicMock()
    download_result.scalar_one_or_none.return_value = download
    settings_result = MagicMock()
    settings_result.scalar_one_or_none.return_value = settings_row
    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None

    calls = {"n": 0}

    def execute(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return download_result
        if calls["n"] == 2:
            return settings_result
        return none_result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute)
    mock_session.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield mock_session

    return ctx


class IntegrityCheckDownloadPathTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def _run_download(self, probe_mock, settings_row=None):
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

            async def fake_download(url, path, dl_id, ses, offset=0):
                Path(path).write_bytes(b"\x47" * 1024)
                return 1024

            with (
                patch(
                    "services.download_manager.async_session_maker",
                    _make_session_ctx(mock_download, settings_row),
                ),
                patch.object(self.manager, "_download_file", fake_download),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_needs_post_processing", return_value=False),
                patch.object(self.manager, "_move_to_completed", side_effect=lambda p, c, d: p),
                patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
                patch.object(self.manager, "_store_recorded_duration", AsyncMock()),
                patch(
                    "services.post_processor.post_processor.probe_media_integrity",
                    probe_mock,
                ),
            ):
                asyncio.run(self.manager._execute_download(1))

            return mock_download, os.path.exists(output_file)

    def test_corrupt_file_completes_with_warning_and_is_kept(self):
        probe = AsyncMock(return_value={
            "checked": True,
            "ok": False,
            "reason": "no audio/video streams were found",
        })
        download, file_exists = self._run_download(probe)

        self.assertEqual(
            download.status,
            DownloadStatus.COMPLETED.value,
            "A suspect file must complete (with warnings), never FAIL.",
        )
        self.assertIn("Completed with warnings", download.error_message)
        self.assertIn("file may be corrupt", download.error_message)
        self.assertIn("no audio/video streams", download.error_message)
        self.assertTrue(file_exists, "The integrity check must never delete the recording.")

    def test_healthy_file_completes_without_warning(self):
        probe = AsyncMock(return_value={"checked": True, "ok": True, "reason": None})
        download, _ = self._run_download(probe)
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.error_message)

    def test_check_skipped_when_disabled_in_settings(self):
        settings_row = AppSettings()
        settings_row.integrity_check_enabled = False
        probe = AsyncMock(return_value={"checked": True, "ok": False, "reason": "bad"})

        download, _ = self._run_download(probe, settings_row=settings_row)

        probe.assert_not_awaited()
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.error_message)

    def test_probe_crash_does_not_break_completion(self):
        probe = AsyncMock(side_effect=RuntimeError("ffprobe exploded"))
        download, file_exists = self._run_download(probe)
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.error_message)
        self.assertTrue(file_exists)


class IntegrityCheckPostProcessPathTests(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()

    def test_post_process_merges_integrity_warning(self):
        """Integrity warning joins the post-processing warning list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "show.ts")
            Path(input_path).write_bytes(b"\x47" * 1024)

            mock_download = MagicMock()
            mock_download.id = 2
            mock_download.status = DownloadStatus.PROCESSING.value
            mock_download.output_path = input_path
            mock_download.progress = 0.0
            mock_download.error_message = None

            async def fake_post_process(path, dl_id, ses, settings):
                return path, []

            probe = AsyncMock(return_value={
                "checked": True,
                "ok": False,
                "reason": "the container could not be parsed",
            })

            with (
                patch(
                    "services.download_manager.async_session_maker",
                    _make_session_ctx(mock_download, None),
                ),
                patch.object(self.manager, "_needs_post_processing", return_value=True),
                patch.object(self.manager, "_post_process", fake_post_process),
                patch.object(self.manager, "_move_to_completed", side_effect=lambda p, c, d: p),
                patch.object(self.manager, "_cleanup_working_files", MagicMock()),
                patch.object(self.manager, "_broadcast_progress", AsyncMock()),
                patch.object(self.manager, "_broadcast_log", AsyncMock()),
                patch.object(self.manager, "_trigger_plex_refresh", AsyncMock()),
                patch.object(self.manager, "_store_recorded_duration", AsyncMock()),
                patch(
                    "services.post_processor.post_processor.probe_media_integrity",
                    probe,
                ),
            ):
                asyncio.run(self.manager._execute_post_process(2))

            self.assertEqual(mock_download.status, DownloadStatus.COMPLETED.value)
            self.assertIn("Completed with warnings", mock_download.error_message)
            self.assertIn("file may be corrupt", mock_download.error_message)
            self.assertTrue(os.path.exists(input_path))


if __name__ == "__main__":
    unittest.main()
