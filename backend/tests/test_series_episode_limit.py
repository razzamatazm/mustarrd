"""
Regression test: SeriesDownloadRequest.episodes must reject requests with more
than 200 episodes.

Before fix: no upper bound, an authenticated user could send 5000+ episodes and
trigger thousands of DB queries in a single request, stalling all other requests
on a Raspberry Pi or low-spec Unraid box.

After fix: Pydantic rejects the payload at validation time with a 422 response.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from api.vod import SeriesDownloadRequest, EpisodeItem


def _episode(n: int) -> dict:
    return {
        "id": f"ep{n}",
        "season": 1,
        "episode_num": n,
        "container_extension": "mp4",
    }


class SeriesEpisodeLimitTests(unittest.TestCase):

    def test_201_episodes_rejected(self):
        """A request with 201 episodes must raise ValidationError."""
        with self.assertRaises(ValidationError):
            SeriesDownloadRequest(
                account_id=1,
                series_id="s1",
                series_name="Test Show",
                episodes=[EpisodeItem(**_episode(i)) for i in range(201)],
            )

    def test_200_episodes_accepted(self):
        """A request with exactly 200 episodes must be valid."""
        req = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Test Show",
            episodes=[EpisodeItem(**_episode(i)) for i in range(200)],
        )
        self.assertEqual(len(req.episodes), 200)

    def test_empty_episodes_accepted(self):
        """An empty episode list must be valid (no episodes to queue)."""
        req = SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Test Show",
            episodes=[],
        )
        self.assertEqual(len(req.episodes), 0)


if __name__ == "__main__":
    unittest.main()
