import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import FileNamer


class SanitizeFilenameTests(unittest.TestCase):
    def test_strips_null_byte(self):
        result = FileNamer.sanitize_filename("The Crown\x00S01E01")
        self.assertNotIn("\x00", result)
        self.assertEqual(result, "The Crown S01E01")

    def test_strips_other_control_chars(self):
        # Tab (\x09), carriage return (\x0d), and other control chars should be removed
        result = FileNamer.sanitize_filename("Show\x09Name\x0dEpisode\x1fTitle")
        for cp in range(0x00, 0x20):
            self.assertNotIn(chr(cp), result)

    def test_null_byte_mid_title_collapses_space(self):
        # Multiple adjacent control chars should collapse to a single space
        result = FileNamer.sanitize_filename("Show\x00\x00Name")
        self.assertEqual(result, "Show Name")

    def test_normal_title_unchanged(self):
        result = FileNamer.sanitize_filename("Breaking Bad S01E01")
        self.assertEqual(result, "Breaking Bad S01E01")

    def test_windows_invalid_chars_still_stripped(self):
        result = FileNamer.sanitize_filename('Bad:Title/Name\\Here?')
        self.assertNotIn(':', result)
        self.assertNotIn('/', result)
        self.assertNotIn('\\', result)
        self.assertNotIn('?', result)

    def test_all_dots_returns_fallback(self):
        # "..." strips to "" -> was producing hidden file ".ts"; must return fallback instead
        result = FileNamer.sanitize_filename("...")
        self.assertEqual(result, "unknown-program")

    def test_all_spaces_returns_fallback(self):
        result = FileNamer.sanitize_filename("   ")
        self.assertEqual(result, "unknown-program")

    def test_all_control_chars_returns_fallback(self):
        result = FileNamer.sanitize_filename("\x00\x00")
        self.assertEqual(result, "unknown-program")

    def test_dots_and_spaces_returns_fallback(self):
        result = FileNamer.sanitize_filename(".. ..")
        self.assertEqual(result, "unknown-program")

    def test_invisible_unicode_only_returns_fallback(self):
        # Zero-width space + zero-width non-joiner -> stripped -> fallback
        result = FileNamer.sanitize_filename("​‌")
        self.assertEqual(result, "unknown-program")

    def test_cjk_long_title_within_200_utf8_bytes(self):
        # 80 CJK chars × 3 bytes/char = 240 bytes; must be truncated to ≤200 bytes
        # Without the fix, 200-char limit passes but 240-byte filename causes ENAMETOOLONG
        title = "中" * 80
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)

    def test_ascii_200_char_limit_preserved(self):
        # ASCII: 1 byte/char, so 200-byte and 200-char limits are identical
        title = "A" * 250
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)
        self.assertEqual(len(result), 200)

    def test_mixed_ascii_cjk_within_200_utf8_bytes(self):
        # Mix of ASCII + CJK where total bytes would exceed 200 without the fix
        title = "Show: " + "中" * 70  # 6 + 210 = 216 bytes
        result = FileNamer.sanitize_filename(title)
        self.assertLessEqual(len(result.encode("utf-8")), 200)


class ExtractSeasonEpisodeTests(unittest.TestCase):
    def test_glued_format(self):
        self.assertEqual(FileNamer.extract_season_episode("S01E01"), (1, 1))

    def test_glued_three_digit_episode(self):
        # Daily shows exceed 99 episodes; old pattern capped at 2 digits
        self.assertEqual(FileNamer.extract_season_episode("S54E173"), (54, 173))

    def test_spaced_format(self):
        # The common EPG format "S54 E173" was not detected at all
        self.assertEqual(FileNamer.extract_season_episode("S54 E173"), (54, 173))

    def test_dot_separator(self):
        self.assertEqual(FileNamer.extract_season_episode("S54.E173"), (54, 173))

    def test_dash_separator(self):
        self.assertEqual(FileNamer.extract_season_episode("S54-E173"), (54, 173))

    def test_comma_separator(self):
        self.assertEqual(FileNamer.extract_season_episode("S04, E12"), (4, 12))

    def test_season_episode_words(self):
        self.assertEqual(FileNamer.extract_season_episode("Season 4 Episode 12"), (4, 12))

    def test_season_episode_words_with_comma(self):
        self.assertEqual(FileNamer.extract_season_episode("Season 4, Episode 12"), (4, 12))

    def test_ep_abbreviation(self):
        self.assertEqual(FileNamer.extract_season_episode("S54 Ep 173"), (54, 173))

    def test_ep_abbreviation_with_period(self):
        self.assertEqual(FileNamer.extract_season_episode("S54 Ep. 173"), (54, 173))

    def test_cross_format(self):
        self.assertEqual(FileNamer.extract_season_episode("4x12"), (4, 12))

    def test_cross_format_three_digit_episode(self):
        self.assertEqual(FileNamer.extract_season_episode("54x173"), (54, 173))

    def test_resolution_not_matched(self):
        # "1280x720" in a description must not parse as season/episode
        self.assertIsNone(FileNamer.extract_season_episode("Broadcast in 1280x720"))

    def test_spaced_x_in_prose_not_matched(self):
        self.assertIsNone(FileNamer.extract_season_episode("a 3 x 4 grid of items"))

    def test_no_marker_returns_none(self):
        self.assertIsNone(FileNamer.extract_season_episode("The Evening Show"))

    def test_marker_in_description(self):
        text = "The Price Is Right S54 E173 Participants must guess the prices"
        self.assertEqual(FileNamer.extract_season_episode(text), (54, 173))


class ExtractShowNameTests(unittest.TestCase):
    def test_glued_marker(self):
        self.assertEqual(FileNamer.extract_show_name("The Crown S01E01 - Hyde Park Corner"), "The Crown")

    def test_spaced_marker(self):
        self.assertEqual(FileNamer.extract_show_name("The Crown S1 E1 - Hyde Park Corner"), "The Crown")

    def test_cross_marker(self):
        self.assertEqual(FileNamer.extract_show_name("The Office 2x04 - The Dundies"), "The Office")

    def test_season_only(self):
        self.assertEqual(FileNamer.extract_show_name("Show Name Season 4"), "Show Name")

    def test_no_marker_unchanged(self):
        self.assertEqual(FileNamer.extract_show_name("The Price Is Right"), "The Price Is Right")


class ExtractEpisodeTitleTests(unittest.TestCase):
    def test_dash_subtitle(self):
        self.assertEqual(
            FileNamer.extract_episode_title("The Crown S01E01 - Hyde Park Corner"),
            "Hyde Park Corner",
        )

    def test_colon_subtitle_spaced_marker(self):
        self.assertEqual(
            FileNamer.extract_episode_title("The Crown S1 E1: Hyde Park Corner"),
            "Hyde Park Corner",
        )

    def test_no_subtitle(self):
        self.assertEqual(FileNamer.extract_episode_title("The Crown S01E01"), "")

    def test_no_marker(self):
        self.assertEqual(FileNamer.extract_episode_title("The Price Is Right"), "")


class DetectSportsTests(unittest.TestCase):
    def test_league_acronym(self):
        self.assertTrue(FileNamer.detect_sports("2026 NBA Finals : San Antonio Spurs at New York Knicks"))

    def test_at_sign(self):
        self.assertTrue(FileNamer.detect_sports("Spurs @ Knicks"))

    def test_vs_word(self):
        self.assertTrue(FileNamer.detect_sports("Team A vs Team B"))

    def test_vs_inside_word_not_matched(self):
        # 'vs' substring in "canvas" used to false-positive
        self.assertFalse(FileNamer.detect_sports("Canvas Painting Masterclass"))

    def test_mma_inside_word_not_matched(self):
        self.assertFalse(FileNamer.detect_sports("Grammar School Stories"))

    def test_sports_category(self):
        self.assertTrue(FileNamer.detect_sports("Some Broadcast", "US Sports"))


class GenerateFilenameTests(unittest.TestCase):
    def setUp(self):
        self.namer = FileNamer()
        self.channel = {"name": "US CBS EAST"}

    def test_spaced_episode_in_description(self):
        # Regression: "S54 E173" in the description must produce an episode filename
        program = {
            "title": "The Price Is Right",
            "description": "S54 E173 Participants must guess the prices of different types of items.",
            "start_time": "2026-06-10T08:00:00",
        }
        result = self.namer.generate_filename(program, self.channel, "tv_show")
        self.assertEqual(result, "The Price Is Right - S54E173.ts")

    def test_episode_with_subtitle_in_title(self):
        program = {
            "title": "The Crown S01E01 - Hyde Park Corner",
            "description": "",
            "start_time": "2026-06-10T20:00:00",
        }
        result = self.namer.generate_filename(program, self.channel, "tv_show")
        self.assertEqual(result, "The Crown - S01E01 - Hyde Park Corner.ts")

    def test_sports_filename(self):
        program = {
            "title": "2026 NBA Finals : San Antonio Spurs at New York Knicks",
            "description": "Game 4 of the NBA Finals.",
            "start_time": "2026-06-10T17:30:00",
        }
        result = self.namer.generate_filename(program, self.channel, "sports")
        self.assertEqual(result, "2026 NBA Finals San Antonio Spurs at New York Knicks - 2026-06-10.ts")


if __name__ == "__main__":
    unittest.main()
