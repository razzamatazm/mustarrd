import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from types import SimpleNamespace

from services.xtream_client import (
    resolve_timeshift_style,
    restyle_timeshift_url,
    timeshift_style_is_automatic,
)

PATH_URL = "http://provider.test:8080/timeshift/user/pass/60/2024-01-15:20-30/123.ts"
QUERY_URL = (
    "http://provider.test:8080/streaming/timeshift.php"
    "?username=user&password=pass&stream=123&start=2024-01-15:20-30&duration=60"
)


def _account(setting, resolved=None):
    return SimpleNamespace(catchup_url_style=setting, catchup_url_style_resolved=resolved)


class ResolveTimeshiftStyleTests(unittest.TestCase):
    def test_explicit_path_wins_over_resolved(self):
        self.assertEqual(resolve_timeshift_style(_account("path", "query")), "path")

    def test_explicit_query_wins_over_resolved(self):
        self.assertEqual(resolve_timeshift_style(_account("query", "path")), "query")

    def test_auto_without_resolved_is_path(self):
        self.assertEqual(resolve_timeshift_style(_account("auto")), "path")

    def test_auto_uses_resolved(self):
        self.assertEqual(resolve_timeshift_style(_account("auto", "query")), "query")

    def test_missing_setting_behaves_like_auto(self):
        self.assertEqual(resolve_timeshift_style(_account(None, "query")), "query")
        self.assertEqual(resolve_timeshift_style(_account("")), "path")

    def test_junk_values_fall_back_to_path(self):
        self.assertEqual(resolve_timeshift_style(_account("nonsense", "nonsense")), "path")

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(resolve_timeshift_style(_account(" Query ")), "query")


class TimeshiftStyleIsAutomaticTests(unittest.TestCase):
    def test_auto_and_unset_and_junk_all_probe(self):
        for setting in ("auto", None, "", "nonsense", " AUTO "):
            self.assertTrue(timeshift_style_is_automatic(_account(setting)), setting)

    def test_a_pinned_account_never_probes(self):
        for setting in ("path", "query", " QUERY "):
            self.assertFalse(timeshift_style_is_automatic(_account(setting)), setting)


class RestyleTimeshiftUrlTests(unittest.TestCase):
    def test_path_to_query(self):
        self.assertEqual(restyle_timeshift_url(PATH_URL, "query"), QUERY_URL)

    def test_query_to_path(self):
        self.assertEqual(restyle_timeshift_url(QUERY_URL, "path"), PATH_URL)

    def test_same_style_is_identity(self):
        self.assertEqual(restyle_timeshift_url(PATH_URL, "path"), PATH_URL)
        self.assertEqual(restyle_timeshift_url(QUERY_URL, "query"), QUERY_URL)

    def test_encoded_credentials_survive_the_round_trip(self):
        original = (
            "http://provider.test:8080/timeshift/us%23er/p%40ss%2Fword/30/2024-01-15:20-30/9%2F9.ts"
        )
        query = restyle_timeshift_url(original, "query")
        self.assertIn("username=us%23er", query)
        self.assertIn("password=p%40ss%2Fword", query)
        self.assertIn("stream=9%2F9", query)
        self.assertEqual(restyle_timeshift_url(query, "path"), original)

    def test_non_timeshift_url_returns_none(self):
        self.assertIsNone(
            restyle_timeshift_url("http://provider.test:8080/live/user/pass/55.ts", "query")
        )
        self.assertIsNone(restyle_timeshift_url("", "query"))
        self.assertIsNone(restyle_timeshift_url(None, "query"))

    def test_malformed_timeshift_path_returns_none(self):
        self.assertIsNone(
            restyle_timeshift_url("http://provider.test:8080/timeshift/user/pass/123.ts", "query")
        )

    def test_query_url_missing_params_returns_none(self):
        self.assertIsNone(
            restyle_timeshift_url(
                "http://provider.test:8080/streaming/timeshift.php?username=user", "path"
            )
        )

    def test_server_path_prefix_is_preserved(self):
        url = "http://host/iptv/timeshift/user/pass/60/2024-01-15:20-30/123.ts"
        self.assertEqual(
            restyle_timeshift_url(url, "query"),
            "http://host/iptv/streaming/timeshift.php"
            "?username=user&password=pass&stream=123&start=2024-01-15:20-30&duration=60",
        )


if __name__ == "__main__":
    unittest.main()
