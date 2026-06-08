"""
Regression test: series_episode_output_path must produce unique paths for
distinct episodes even when all available metadata is identical (season=0,
episode_num=0, no episode title).

Non-conforming IPTV providers commonly return episodes with no season or
episode numbers and no episode title. When a user selects multiple such
episodes for download, build_episode_download() calls
series_episode_output_path() for each with the same metadata values.
Without the episode_id fallback, all calls produce the same output path.

Also covers negative season/episode values: providers sometimes send -1 for
unknown values. Before the fix, these were clamped to 0, colliding with
genuine season=0/episode=0 entries and expanding the corruption surface.
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
                episode_id=ep.get("id"),
            )
            for ep in episodes
        ]

    def test_multiple_zero_season_zero_episode_no_title_unique_paths(self):
        """
        Three distinct provider episodes with season=0, episode_num=0, no title
        must produce three unique output paths when episode IDs differ.
        """
        episodes = [
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None, "id": "ep1"},
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None, "id": "ep2"},
            {"show_name": "My Show", "season": 0, "episode_num": 0, "title": None, "id": "ep3"},
        ]
        paths = self._batch_paths(episodes)
        unique_paths = set(paths)
        self.assertEqual(
            len(unique_paths),
            len(paths),
            f"Output path collision: {len(paths) - len(unique_paths)} episodes share a path. "
            f"Paths: {paths}",
        )

    def test_two_episodes_same_zero_metadata_unique_paths(self):
        """
        Two distinct provider episodes with season=0, episode_num=0, no title
        must produce different output paths when episode IDs differ.
        """
        path_a = series_episode_output_path("/downloads", "Drama Series", 0, 0, None, "mp4", episode_id="e100")
        path_b = series_episode_output_path("/downloads", "Drama Series", 0, 0, None, "mp4", episode_id="e101")
        self.assertNotEqual(
            path_a,
            path_b,
            f"Both episodes map to {path_a!r}; second download silently "
            "overwrites the first.",
        )

    def test_negative_season_episode_not_clamped_to_zero(self):
        """
        season=-1 and episode_num=-1 must produce paths distinct from season=0
        and episode_num=0. Previously these were clamped to 0, expanding the
        collision surface.
        """
        path_negative = series_episode_output_path("/downloads", "My Show", -1, -1, None, "mkv")
        path_zero = series_episode_output_path("/downloads", "My Show", 0, 0, None, "mkv")
        self.assertNotEqual(
            path_negative,
            path_zero,
            f"season=-1/episode_num=-1 and season=0/episode_num=0 both produce "
            f"{path_zero!r}. Negative provider values collide with zero-indexed entries.",
        )


if __name__ == "__main__":
    unittest.main()
