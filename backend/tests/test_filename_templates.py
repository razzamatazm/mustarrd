import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.file_namer import FileNamer


def _prog(title, description="", start_time="2024-03-15T20:00:00"):
    return {"title": title, "description": description, "start_time": start_time}


def _channel(name="CNN"):
    return {"name": name, "category_name": ""}


class TemplateWiringTests(unittest.TestCase):
    """User-configured templates are applied to generated filenames."""

    def test_default_template_applied(self):
        settings = {"default_template": "{channel} - {title} - {date}"}
        result = FileNamer().generate_filename(_prog("Evening News"), _channel("CNN"), "other", settings)
        self.assertEqual(result, "CNN - Evening News - 2024-03-15.ts")

    def test_sports_template_applied(self):
        settings = {"sports_template": "{title} ({date})"}
        result = FileNamer().generate_filename(_prog("Lakers vs Celtics"), _channel(), "sports", settings)
        self.assertEqual(result, "Lakers vs Celtics (2024-03-15).ts")

    def test_movie_template_applied(self):
        settings = {"movie_template": "{title} [{year}]"}
        result = FileNamer().generate_filename(_prog("Inception", "A 2010 film"), _channel(), "movie", settings)
        self.assertEqual(result, "Inception [2010].ts")

    def test_tv_template_applied_when_season_episode_detected(self):
        settings = {"tv_template": "{show} {season}x{episode:02d}"}
        result = FileNamer().generate_filename(
            _prog("Breaking Bad S02E05 - Breakage"), _channel(), "tv_show", settings
        )
        self.assertEqual(result, "Breaking Bad 2x05.ts")

    def test_tv_falls_back_to_default_template_when_no_season_episode(self):
        settings = {"default_template": "{title} ({date})"}
        result = FileNamer().generate_filename(_prog("Evening News"), _channel(), "tv_show", settings)
        self.assertEqual(result, "Evening News (2024-03-15).ts")

    def test_no_settings_uses_hardcoded_defaults(self):
        result = FileNamer().generate_filename(_prog("Evening News"), _channel(), "other")
        self.assertEqual(result, "Evening News - 2024-03-15.ts")

    def test_bad_template_key_falls_back_gracefully(self):
        settings = {"default_template": "{title} - {nonexistent_key}"}
        result = FileNamer().generate_filename(_prog("Evening News"), _channel(), "other", settings)
        self.assertEqual(result, "Evening News - 2024-03-15.ts")

    def test_channel_variable_available_in_template(self):
        settings = {"default_template": "{channel}/{title}"}
        result = FileNamer().generate_filename(_prog("The Wire"), _channel("HBO"), "other", settings)
        self.assertEqual(result, "HBO The Wire.ts")

    def test_tv_no_subtitle_default_omits_title_segment(self):
        # EPG title with no episode subtitle must not duplicate the show name.
        # "Breaking Bad S01E01" has no "- Episode Title" part, so the output
        # must be "Breaking Bad - S01E01.ts", not "Breaking Bad - S01E01 - Breaking Bad S01E01.ts".
        result = FileNamer().generate_filename(
            _prog("Breaking Bad S01E01"), _channel(), "tv_show"
        )
        self.assertEqual(result, "Breaking Bad - S01E01.ts")

    def test_tv_no_subtitle_with_default_tv_template_set(self):
        # AppSettings always ships a default tv_template, so settings will always
        # have tv_template set. The no-subtitle branch must still be used when
        # episode_title is empty, even when tv_template is present.
        settings = {"tv_template": "{show} - S{season:02d}E{episode:02d} - {title}"}
        result = FileNamer().generate_filename(
            _prog("Breaking Bad S01E01"), _channel(), "tv_show", settings
        )
        self.assertEqual(result, "Breaking Bad - S01E01.ts")

    def test_tv_with_subtitle_includes_episode_title(self):
        result = FileNamer().generate_filename(
            _prog("Breaking Bad S01E01 - Pilot"), _channel(), "tv_show"
        )
        self.assertEqual(result, "Breaking Bad - S01E01 - Pilot.ts")


class RemovesuffixTests(unittest.TestCase):
    """removesuffix fix: .ts in the stem is not consumed."""

    def test_ts_in_stem_preserved_download_builder(self):
        from services.file_namer import file_namer
        name = "KTSA.ts Evening News"
        result = file_namer.sanitize_filename(name.removesuffix(".ts")) + ".ts"
        self.assertIn("KTSA", result)
        self.assertTrue(result.endswith(".ts"))

    def test_ts_only_at_end_stripped_once(self):
        from services.file_namer import file_namer
        name = "My Show.ts"
        result = file_namer.sanitize_filename(name.removesuffix(".ts")) + ".ts"
        self.assertEqual(result, "My Show.ts")


if __name__ == "__main__":
    unittest.main()
