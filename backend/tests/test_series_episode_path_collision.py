"""
Regression test: series_episode_output_path must produce unique paths for
distinct episodes even when all available metadata is identical (season=0,
episode_num=0, no episode title).

Non-conforming IPTV providers commonly return episodes with no season or
episode numbers and no episode title. When a user selects multiple such
episodes for download, build_episode_download() calls
series_episode_output_path() for each with the same metadata values.
All calls produce the same output path, for example:

    /downloads/My Show/Season 00/S00E00 - My Show.mkv

The second and subsequent downloads silently overwrite or corrupt the first.
The user sees N "Completed" entries in the Downloads list but only one actual
file on disk. No error is raised.

Fix requires series_episode_output_path to accept an episode_id fallback so
it can produce unique filenames when season/episode_num/title are all missing.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.vod_namer import series_episode_output_path


class SeriesEpisodePathCollisionTests(unittest.TestCase):

    def _batch_paths(self, episodes):
        """Simulate what vod.py:download_series does when building output paths."""
        return [
            series_episode_output_path(
                "/downloads",
                ep["show_name"],
                ep["season"],
                ep["episode_num"],
                ep.get("title"),
                ep.get("extension", "mkv"),
            )
            for ep in episodes
        ]

    def test_multiple_zero_season_zero_episode_no_title_unique_paths(self):
        """
        Three distinct provider episodes with season=0, episode_num=0, no title
        must produce three unique output paths. Currently all map to the same
        path, causing silent overwrite of completed downloads.
        """
        episodes = [
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None},
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None},
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None},
        ]
        paths = self._batch_paths(episodes)
        unique_paths = set(paths)
        self.assertEqual(
            len(unique_paths),
            len(paths),
            f"Output path collision: all {len(paths)} episodes map to "
            f"{next(iter(unique_paths))!r}. Each episode must have a unique path.",
        )

    def test_two_episodes_same_zero_metadata_unique_paths(self):
        """
        Two distinct provider episodes with season=0, episode_num=0, no title
        must produce different output paths.
        """
        path_a = series_episode_output_path("/downloads", "Drama Series", 0, 0, None, "mp4")
        path_b = series_episode_output_path("/downloads", "Drama Series", 0, 0, None, "mp4")
        self.assertNotEqual(
            path_a,
            path_b,
            f"Both episodes map to {path_a!r}; second download silently "
            "overwrites the first.",
        )

    def test_negative_season_episode_treated_as_zero_still_collides(self):
        """
        Providers sending season=-1 or episode_num=-1 are clamped to 0 by
        series_episode_output_path, producing the same path as genuine 0/0
        episodes and exacerbating the collision.
        """
        path_negative = series_episode_output_path("/downloads", "My Show", -1, -1, None, "mkv")
        path_zero = series_episode_output_path("/downloads", "My Show", 0, 0, None, "mkv")
        self.assertNotEqual(
            path_negative,
            path_zero,
            f"season=-1/episode_num=-1 and season=0/episode_num=0 both produce "
            f"{path_zero!r}. Negative values from the provider expand the collision surface.",
        )


if __name__ == "__main__":
    unittest.main()
