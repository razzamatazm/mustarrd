import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.epg_service import epg_service


class DetectProgramTypeTests(unittest.TestCase):
    def _detect(self, title, description="", category=None):
        program = {"title": title, "description": description}
        channel = {"category_name": category} if category is not None else None
        return epg_service.detect_program_type(program, channel)

    def test_spaced_episode_in_description_is_tv_show(self):
        # Regression: "S54 E173" (spaced) was classified as "other"
        result = self._detect(
            "The Price Is Right",
            "S54 E173 Participants must guess the prices of different types of items.",
        )
        self.assertEqual(result, "tv_show")

    def test_glued_episode_is_tv_show(self):
        self.assertEqual(self._detect("The Crown S01E01 - Hyde Park Corner"), "tv_show")

    def test_three_digit_episode_is_tv_show(self):
        self.assertEqual(self._detect("Show", "S54E173"), "tv_show")

    def test_episode_only_marker_is_tv_show(self):
        self.assertEqual(self._detect("Late Night Show", "Ep 173"), "tv_show")

    def test_nba_title_is_sports(self):
        result = self._detect(
            "2026 NBA Finals : San Antonio Spurs at New York Knicks",
            "The Knicks host the Spurs at Madison Square Garden for Game 4 of the NBA Finals.",
        )
        self.assertEqual(result, "sports")

    def test_sports_channel_category(self):
        self.assertEqual(self._detect("Some Broadcast", category="US Sports"), "sports")

    def test_vs_inside_word_not_sports(self):
        self.assertEqual(self._detect("Canvas Painting Masterclass"), "other")

    def test_mma_inside_word_not_sports(self):
        self.assertEqual(self._detect("Grammar School Stories"), "other")

    def test_roman_numeral_not_sports(self):
        self.assertEqual(self._detect("Henry V"), "other")

    def test_news_word_boundary(self):
        self.assertEqual(self._detect("Evening News"), "news")
        # "news" inside "Newsroom" must not match
        self.assertEqual(self._detect("The Newsroom"), "other")

    def test_movie_category(self):
        self.assertEqual(self._detect("Heat", category="Movies"), "movie")

    def test_none_description(self):
        # Programs with a null description must not crash detection
        self.assertEqual(epg_service.detect_program_type({"title": "Show", "description": None}), "other")

    def test_plain_program_is_other(self):
        self.assertEqual(self._detect("Morning Talk"), "other")


if __name__ == "__main__":
    unittest.main()
