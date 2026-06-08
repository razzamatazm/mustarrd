"""
Regression test: series_episode_output_path episode_id fallback too narrow.

Bug: The episode_id dedup guard fires only when season_num == 0 AND episode_num == 0.
Two distinct provider episodes with season=N (N>0), episode_num=0, and no title
produce the same output path. The second download silently overwrites the first.

Root cause in vod_namer.py:
    elif season_num == 0 and episode_num == 0 and episode_id:
        base_name = f"{base_name} - {_sanitize_component(str(episode_id))}"

The season_num == 0 guard means season=2, episode=0 never reaches the fallback.
"""
import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "vod_namer.py"
_SPEC = importlib.util.spec_from_file_location("vod_namer_ep0_season_n_test", _MODULE_PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)

series_episode_output_path = _MOD.series_episode_output_path


class VodNamerEp0SeasonNCollisionTests(unittest.TestCase):
    def test_season_n_episode_zero_no_title_unique_paths(self):
        """season=2, ep=0, no title, different IDs must not produce the same path.

        This is the failing case: the episode_id fallback only fires for (0,0),
        so two extras in season 2 with no title collide.
        """
        path_a = series_episode_output_path(
            "/dl", "My Show", 2, 0, None, "mp4", episode_id="ep_aaa"
        )
        path_b = series_episode_output_path(
            "/dl", "My Show", 2, 0, None, "mp4", episode_id="ep_bbb"
        )
        self.assertNotEqual(
            path_a,
            path_b,
            f"Collision at {path_a!r}: episode_id fallback not applied for season=2, "
            "episode=0. Both files map to the same path; second download overwrites first.",
        )

    def test_season_1_episode_zero_no_title_unique_paths(self):
        """Same collision is reproducible with season=1."""
        path_a = series_episode_output_path(
            "/dl", "Another Show", 1, 0, None, "mkv", episode_id="id1"
        )
        path_b = series_episode_output_path(
            "/dl", "Another Show", 1, 0, None, "mkv", episode_id="id2"
        )
        self.assertNotEqual(
            path_a,
            path_b,
            f"Collision at {path_a!r}: season=1, episode=0 also affected.",
        )

    def test_season_zero_episode_zero_still_unique(self):
        """The original (0,0) fix must still hold after any correction."""
        path_a = series_episode_output_path(
            "/dl", "My Show", 0, 0, None, "mp4", episode_id="ep1"
        )
        path_b = series_episode_output_path(
            "/dl", "My Show", 0, 0, None, "mp4", episode_id="ep2"
        )
        self.assertNotEqual(
            path_a,
            path_b,
            "Regression: (0,0) case should still produce unique paths.",
        )

    def test_numbered_episode_no_title_does_not_get_id_suffix(self):
        """Untitled but properly numbered episodes (e.g. S02E05) must not get episode_id appended.

        SxxExx already disambiguates; appending the provider ID would corrupt
        Plex/Jellyfin filenames for providers that omit episode titles.
        """
        path = series_episode_output_path(
            "/dl", "My Show", 2, 5, None, "mp4", episode_id="48213"
        )
        self.assertNotIn(
            "48213",
            path,
            f"episode_id must not appear in path for a numbered episode (S02E05): {path!r}",
        )

    def test_episode_with_title_not_affected(self):
        """Episodes with a title must not be modified by the fallback."""
        path_a = series_episode_output_path(
            "/dl", "My Show", 2, 0, "Bonus Clip", "mp4", episode_id="ep1"
        )
        path_b = series_episode_output_path(
            "/dl", "My Show", 2, 0, "Bonus Clip", "mp4", episode_id="ep2"
        )
        # Same title -> same path is acceptable (title already disambiguates show vs show,
        # but two episodes with identical title/season/ep numbers genuinely do collide;
        # that is a pre-existing separate concern).
        # The important thing here is the title-present path does NOT include episode_id.
        self.assertNotIn(
            "ep1",
            path_a,
            "episode_id must not appear in path when episode title is present.",
        )


if __name__ == "__main__":
    unittest.main()
