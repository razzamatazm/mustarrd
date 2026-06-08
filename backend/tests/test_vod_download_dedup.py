"""
Regression tests: VOD download endpoints must return 409 when a duplicate download
is already active for the same output file.

Before fix: queue_download raises ValueError when an active (PENDING/DOWNLOADING/
PROCESSING) download exists for the same output_path. That ValueError was not caught
in vod.py, so both download_movie and download_series returned HTTP 500 instead of
409. A user who double-clicked Download (or sent two parallel requests) got a 500
error and two concurrent write tasks corrupting the same output file.

After fix: both endpoints wrap queue_download in try/except ValueError and raise
HTTPException(status_code=409).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException


def _make_auth():
    auth = MagicMock()
    auth.user_id = 1
    auth.provider = "admin_local"
    auth.is_admin = True
    return auth


def _make_session():
    session = AsyncMock()
    return session


def _fake_download(output_path="/downloads/Die Hard (1988).mp4"):
    d = MagicMock()
    d.to_dict.return_value = {"id": 1, "output_path": output_path, "status": "completed"}
    return d


class MovieDownloadDedupTests(unittest.IsolatedAsyncioTestCase):
    """download_movie returns 409 when queue_download detects a conflict."""

    async def test_duplicate_movie_returns_409(self):
        """Second download of the same movie returns 409, not 500.

        Before fix: ValueError from queue_download propagated as HTTP 500.
        After fix: ValueError is caught and re-raised as HTTP 409.
        """
        from api.vod import download_movie, MovieDownloadRequest

        data = MovieDownloadRequest(account_id=1, vod_id="42", name="Die Hard",
                                    container_extension="mp4")

        with patch("api.vod.build_movie_download", new=AsyncMock(return_value=_fake_download())), \
             patch("api.vod.check_disk_space", new=AsyncMock()), \
             patch("api.vod.download_manager.queue_download",
                   new=AsyncMock(side_effect=ValueError("already active for this output file: Die Hard (1988).mp4"))):
            with self.assertRaises(HTTPException) as ctx:
                await download_movie(data=data, auth=_make_auth(), session=_make_session())

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_first_movie_download_succeeds(self):
        """First download with no existing active download proceeds normally."""
        from api.vod import download_movie, MovieDownloadRequest

        data = MovieDownloadRequest(account_id=1, vod_id="42", name="Die Hard",
                                    container_extension="mp4")
        fake = _fake_download()

        with patch("api.vod.build_movie_download", new=AsyncMock(return_value=fake)), \
             patch("api.vod.check_disk_space", new=AsyncMock()), \
             patch("api.vod.download_manager.queue_download", new=AsyncMock(return_value=fake)):
            result = await download_movie(data=data, auth=_make_auth(), session=_make_session())

        self.assertIn("id", result)


class SeriesDownloadDedupTests(unittest.IsolatedAsyncioTestCase):
    """download_series returns 409 when queue_download detects a conflict."""

    async def test_duplicate_episode_returns_409(self):
        """Second download of the same episode returns 409, not 500.

        Before fix: ValueError from queue_download propagated as HTTP 500.
        After fix: ValueError is caught and re-raised as HTTP 409.
        """
        from api.vod import download_series, SeriesDownloadRequest, EpisodeItem

        data = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Breaking Bad",
            episodes=[EpisodeItem(id="e1", season=1, episode_num=1, title="Pilot")],
        )

        with patch("api.vod.build_episode_download", new=AsyncMock(return_value=_fake_download())), \
             patch("api.vod.check_disk_space", new=AsyncMock()), \
             patch("api.vod.download_manager.queue_download",
                   new=AsyncMock(side_effect=ValueError("already active for this output file: S01E01.mp4"))):
            with self.assertRaises(HTTPException) as ctx:
                await download_series(data=data, auth=_make_auth(), session=_make_session())

        self.assertEqual(ctx.exception.status_code, 409)

    async def test_first_episode_download_succeeds(self):
        """First episode download with no conflict proceeds normally."""
        from api.vod import download_series, SeriesDownloadRequest, EpisodeItem

        data = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Breaking Bad",
            episodes=[EpisodeItem(id="e1", season=1, episode_num=1, title="Pilot")],
        )
        fake = _fake_download()

        with patch("api.vod.build_episode_download", new=AsyncMock(return_value=fake)), \
             patch("api.vod.check_disk_space", new=AsyncMock()), \
             patch("api.vod.download_manager.queue_download", new=AsyncMock(return_value=fake)):
            result = await download_series(data=data, auth=_make_auth(), session=_make_session())

        self.assertEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main()
