import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.responses import FileResponse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.downloads import get_download_hls_asset, get_download_playback_info
from auth import AuthContext
from models import AppSettings, Download, DownloadStatus
from services.hls_streamer import HLSSession, HLSStartError


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _make_download(output_path, status=DownloadStatus.COMPLETED.value):
    dl = Download()
    dl.id = 1
    dl.status = status
    dl.output_path = output_path
    dl.requested_by_user_id = None
    return dl


def _session_for(download, folder):
    db_settings = AppSettings()
    db_settings.download_folder = folder
    db_settings.completed_folder = folder
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _ScalarResult(download),
        _ScalarResult(db_settings),
    ])
    return session


ADMIN = AuthContext(authenticated=True, is_admin=True, user_id=1)


class HLSAssetValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_asset_404_without_db_lookup(self):
        session = AsyncMock()
        for asset in ["../playlist.m3u8", "ffmpeg.log", "seg1.m4s", "evil.m4s"]:
            with self.assertRaises(HTTPException) as ctx:
                await get_download_hls_asset(1, asset, auth=ADMIN, session=session)
            self.assertEqual(ctx.exception.status_code, 404, asset)
        session.execute.assert_not_called()

    async def test_download_not_found_404(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[_ScalarResult(None)])
        with self.assertRaises(HTTPException) as ctx:
            await get_download_hls_asset(1, "playlist.m3u8", auth=ADMIN, session=session)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_not_completed_409(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[
            _ScalarResult(_make_download("/x.ts", status=DownloadStatus.DOWNLOADING.value)),
        ])
        with self.assertRaises(HTTPException) as ctx:
            await get_download_hls_asset(1, "playlist.m3u8", auth=ADMIN, session=session)
        self.assertEqual(ctx.exception.status_code, 409)


class HLSPlaylistTests(unittest.IsolatedAsyncioTestCase):
    async def test_playlist_starts_session_and_serves_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            hls_dir = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(hls_dir, ignore_errors=True))
            hls_session = HLSSession(key="download:1", source_path=Path(source), directory=hls_dir)
            hls_session.playlist_path.write_text("#EXTM3U\nseg00000.m4s\n")

            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_or_create_file = AsyncMock(return_value=hls_session)
                mock_streamer.wait_for_playlist = AsyncMock()

                result = await get_download_hls_asset(
                    1, "playlist.m3u8", start=0.0, auth=ADMIN, session=session
                )

            self.assertIsInstance(result, FileResponse)
            self.assertEqual(result.media_type, "application/vnd.apple.mpegurl")
            mock_streamer.get_or_create_file.assert_awaited_once()
            # No explicit start: session begins at the top of the file.
            self.assertEqual(mock_streamer.get_or_create_file.await_args.args[2], 0.0)

    async def test_playlist_passes_start_offset_to_session(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            hls_dir = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(hls_dir, ignore_errors=True))
            hls_session = HLSSession(
                key="download:1", source_path=Path(source), directory=hls_dir, start_offset=300.0
            )
            hls_session.playlist_path.write_text("#EXTM3U\nseg00000.m4s\n")

            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_or_create_file = AsyncMock(return_value=hls_session)
                mock_streamer.wait_for_playlist = AsyncMock()

                await get_download_hls_asset(
                    1, "playlist.m3u8", start=300.0, auth=ADMIN, session=session
                )

            self.assertEqual(mock_streamer.get_or_create_file.await_args.args[2], 300.0)

    async def test_ffmpeg_failure_maps_to_502(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_or_create_file = AsyncMock(side_effect=HLSStartError("FFmpeg failed"))

                with self.assertRaises(HTTPException) as ctx:
                    await get_download_hls_asset(1, "playlist.m3u8", auth=ADMIN, session=session)
                self.assertEqual(ctx.exception.status_code, 502)


class HLSSegmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_segment_without_active_session_409(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_active.return_value = None

                with self.assertRaises(HTTPException) as ctx:
                    await get_download_hls_asset(1, "seg00000.m4s", auth=ADMIN, session=session)
                self.assertEqual(ctx.exception.status_code, 409)

    async def test_segment_served_and_session_touched(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            hls_dir = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(hls_dir, ignore_errors=True))
            hls_session = HLSSession(key="download:1", source_path=Path(source), directory=hls_dir)
            (hls_dir / "seg00000.m4s").write_bytes(b"segment")

            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_active.return_value = hls_session

                result = await get_download_hls_asset(1, "seg00000.m4s", auth=ADMIN, session=session)

            self.assertIsInstance(result, FileResponse)
            self.assertEqual(result.media_type, "video/iso.segment")
            mock_streamer.touch.assert_called_once_with(hls_session)

    async def test_missing_segment_file_404(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.ts")
            Path(source).write_bytes(b"fake")
            hls_dir = Path(tempfile.mkdtemp())
            self.addCleanup(lambda: __import__("shutil").rmtree(hls_dir, ignore_errors=True))
            hls_session = HLSSession(key="download:1", source_path=Path(source), directory=hls_dir)

            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.get_active.return_value = hls_session

                with self.assertRaises(HTTPException) as ctx:
                    await get_download_hls_asset(1, "seg00000.m4s", auth=ADMIN, session=session)
                self.assertEqual(ctx.exception.status_code, 404)


class PlaybackInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_probed_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.mkv")
            Path(source).write_bytes(b"fake")
            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.probe_duration = AsyncMock(return_value=2641.47)

                result = await get_download_playback_info(1, auth=ADMIN, session=session)

            self.assertEqual(result, {"duration": 2641.47})

    async def test_unprobeable_file_returns_null_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "Show.mkv")
            Path(source).write_bytes(b"fake")
            session = _session_for(_make_download(source), folder)
            with patch("api.downloads.settings") as mock_cfg, \
                    patch("api.downloads.hls_streamer") as mock_streamer:
                mock_cfg.default_download_folder = folder
                mock_cfg.default_completed_folder = folder
                mock_streamer.probe_duration = AsyncMock(return_value=None)

                result = await get_download_playback_info(1, auth=ADMIN, session=session)

            self.assertEqual(result, {"duration": None})


if __name__ == "__main__":
    unittest.main()
