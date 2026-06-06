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

from api.downloads import get_download_file
from auth import AuthContext
from models import AppSettings, Download, DownloadStatus


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


class DownloadFileAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_in_db_custom_folder_not_403(self):
        """File in DB-configured folder is served even when it differs from the env default."""
        with tempfile.TemporaryDirectory() as custom_dir:
            custom_file = os.path.join(custom_dir, "Show.ts")
            Path(custom_file).write_bytes(b"fake")

            db_settings = AppSettings()
            db_settings.download_folder = custom_dir
            db_settings.completed_folder = "/some/other/completed"

            session = AsyncMock()
            session.execute = AsyncMock(side_effect=[
                _ScalarResult(_make_download(custom_file)),
                _ScalarResult(db_settings),
            ])

            auth = AuthContext(authenticated=True, is_admin=True, user_id=1)

            with patch("api.downloads.settings") as mock_cfg:
                mock_cfg.default_download_folder = "/app/downloads"
                mock_cfg.default_completed_folder = "/app/completed"

                result = await get_download_file(1, None, "download", auth, session)
                self.assertIsInstance(result, FileResponse)

    async def test_env_fallback_when_no_db_row(self):
        """Falls back to env defaults when there is no AppSettings row."""
        with tempfile.TemporaryDirectory() as default_dir:
            default_file = os.path.join(default_dir, "Show.ts")
            Path(default_file).write_bytes(b"fake")

            session = AsyncMock()
            session.execute = AsyncMock(side_effect=[
                _ScalarResult(_make_download(default_file)),
                _ScalarResult(None),
            ])

            auth = AuthContext(authenticated=True, is_admin=True, user_id=1)

            with patch("api.downloads.settings") as mock_cfg:
                mock_cfg.default_download_folder = default_dir
                mock_cfg.default_completed_folder = "/some/completed"

                result = await get_download_file(1, None, "download", auth, session)
                self.assertIsInstance(result, FileResponse)

    async def test_file_in_env_default_while_db_points_elsewhere_not_403(self):
        """File under the original env-default folder is still served after user switches to a
        custom folder in Settings. Previously the allowlist dropped the env-default roots once
        a DB row existed, causing a 403 on every recording made before the folder change."""
        with tempfile.TemporaryDirectory() as env_default_dir:
            old_file = os.path.join(env_default_dir, "OldShow.ts")
            Path(old_file).write_bytes(b"fake")

            db_settings = AppSettings()
            db_settings.download_folder = "/nas/downloads"
            db_settings.completed_folder = "/nas/completed"

            session = AsyncMock()
            session.execute = AsyncMock(side_effect=[
                _ScalarResult(_make_download(old_file)),
                _ScalarResult(db_settings),
            ])

            auth = AuthContext(authenticated=True, is_admin=True, user_id=1)

            with patch("api.downloads.settings") as mock_cfg:
                mock_cfg.default_download_folder = env_default_dir
                mock_cfg.default_completed_folder = "/app/completed"

                result = await get_download_file(1, None, "download", auth, session)
                self.assertIsInstance(result, FileResponse)

    async def test_path_outside_all_roots_returns_403(self):
        """File path outside DB folders and env defaults raises 403."""
        with tempfile.TemporaryDirectory() as allowed_dir:
            with tempfile.TemporaryDirectory() as outside_dir:
                outside_file = os.path.join(outside_dir, "Show.ts")

                db_settings = AppSettings()
                db_settings.download_folder = allowed_dir
                db_settings.completed_folder = allowed_dir

                session = AsyncMock()
                session.execute = AsyncMock(side_effect=[
                    _ScalarResult(_make_download(outside_file)),
                    _ScalarResult(db_settings),
                ])

                auth = AuthContext(authenticated=True, is_admin=True, user_id=1)

                with patch("api.downloads.settings") as mock_cfg:
                    mock_cfg.default_download_folder = allowed_dir
                    mock_cfg.default_completed_folder = allowed_dir

                    with self.assertRaises(HTTPException) as ctx:
                        await get_download_file(1, None, "download", auth, session)

                    self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
