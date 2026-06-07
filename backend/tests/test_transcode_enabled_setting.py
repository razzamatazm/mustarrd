"""
Regression test: transcode_enabled=False must bypass ffmpeg post-processing.

Bug: _needs_post_processing() always returned True for non-VOD downloads regardless
of transcode_enabled/comskip_enabled, and will_transcode in _execute_post_process()
ignored settings.transcode_enabled entirely. Every catchup download was silently
remuxed to .mkv even when the user disabled transcoding.

Reproduction:
  1. Settings > disable transcoding (transcode_enabled=False, comskip_enabled=False).
  2. Download any catchup recording (ffmpeg installed, standard Docker image).
  3. Logs show "Remuxing to mkv..." and the file is .mkv, not original .ts.

Expected: transcode_enabled=False skips ffmpeg; raw .ts moves straight to completed.
Actual:   ffmpeg ran on every catchup download regardless of the setting.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.download_manager import DownloadManager


def _make_settings(transcode_enabled: bool, comskip_enabled: bool):
    s = MagicMock()
    s.transcode_enabled = transcode_enabled
    s.comskip_enabled = comskip_enabled
    return s


def _make_download(is_vod: bool = False):
    d = MagicMock()
    d.is_vod = is_vod
    return d


class TranscodeEnabledSettingTests(unittest.TestCase):
    def setUp(self):
        self.dm = DownloadManager()

    def test_no_post_processing_when_both_disabled(self):
        """_needs_post_processing must return False when transcode and comskip are both off.

        Before fix: always returned True, causing ffmpeg to run on every download.
        After fix: returns False, skipping post-processing entirely.
        """
        settings = _make_settings(transcode_enabled=False, comskip_enabled=False)
        download = _make_download(is_vod=False)
        self.assertFalse(
            self.dm._needs_post_processing(download, settings),
            "_needs_post_processing must return False when transcode_enabled=False "
            "and comskip_enabled=False; ffmpeg should not run.",
        )

    def test_post_processing_when_transcode_enabled(self):
        """_needs_post_processing returns True when transcode_enabled=True (default)."""
        settings = _make_settings(transcode_enabled=True, comskip_enabled=False)
        download = _make_download(is_vod=False)
        self.assertTrue(
            self.dm._needs_post_processing(download, settings),
            "_needs_post_processing must return True when transcode_enabled=True.",
        )

    def test_post_processing_when_comskip_enabled(self):
        """_needs_post_processing returns True when comskip_enabled=True (needs ffmpeg)."""
        settings = _make_settings(transcode_enabled=False, comskip_enabled=True)
        download = _make_download(is_vod=False)
        self.assertTrue(
            self.dm._needs_post_processing(download, settings),
            "_needs_post_processing must return True when comskip_enabled=True "
            "even if transcode_enabled=False.",
        )

    def test_vod_never_needs_post_processing(self):
        """VOD downloads skip post-processing regardless of settings (existing behaviour)."""
        settings = _make_settings(transcode_enabled=True, comskip_enabled=True)
        download = _make_download(is_vod=True)
        self.assertFalse(
            self.dm._needs_post_processing(download, settings),
            "VOD downloads must never trigger post-processing.",
        )

    def test_no_settings_skips_post_processing(self):
        """_needs_post_processing returns False when settings is None (existing behaviour)."""
        download = _make_download(is_vod=False)
        self.assertFalse(
            self.dm._needs_post_processing(download, None),
            "_needs_post_processing must return False when settings is None.",
        )


if __name__ == "__main__":
    unittest.main()
