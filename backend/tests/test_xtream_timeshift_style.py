import sys
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


class TimeshiftUrlStyleTests(unittest.TestCase):
    def _client(self, username="user", password="pass"):
        return XtreamClient("http://provider.test:8080", username, password)

    def _ts(self):
        return datetime(2024, 1, 15, 20, 30)

    def test_default_style_is_path_and_unchanged(self):
        url = self._client().build_timeshift_url("123", self._ts(), 60)
        self.assertEqual(
            url,
            "http://provider.test:8080/timeshift/user/pass/60/2024-01-15:20-30/123.ts",
        )

    def test_explicit_path_style_matches_default(self):
        client = self._client()
        self.assertEqual(
            client.build_timeshift_url("123", self._ts(), 60, style="path"),
            client.build_timeshift_url("123", self._ts(), 60),
        )

    def test_query_style_exact_string(self):
        url = self._client().build_timeshift_url("123", self._ts(), 60, style="query")
        self.assertEqual(
            url,
            "http://provider.test:8080/streaming/timeshift.php"
            "?username=user&password=pass&stream=123&start=2024-01-15:20-30&duration=60",
        )

    def test_query_style_prefers_provider_start(self):
        url = self._client().build_timeshift_url(
            "77", self._ts(), 90, provider_start="2024-02-02:01-05", style="query"
        )
        self.assertIn("start=2024-02-02:01-05", url)
        self.assertNotIn("2024-01-15", url)

    def test_path_style_prefers_provider_start(self):
        url = self._client().build_timeshift_url(
            "77", self._ts(), 90, provider_start="2024-02-02:01-05", style="path"
        )
        self.assertIn("/2024-02-02:01-05/", url)

    def test_unknown_style_falls_back_to_path(self):
        client = self._client()
        self.assertEqual(
            client.build_timeshift_url("123", self._ts(), 60, style="nonsense"),
            client.build_timeshift_url("123", self._ts(), 60, style="path"),
        )

    # --- query-form encoding, mirroring the path-form encoding tests ---

    def test_query_hash_in_password_encoded(self):
        url = self._client(password="p@ss#word").build_timeshift_url(
            "123", self._ts(), 60, style="query"
        )
        self.assertIn("password=p%40ss%23word", url)
        self.assertNotIn("#", url)

    def test_query_hash_in_username_encoded(self):
        url = self._client(username="us#er").build_timeshift_url(
            "123", self._ts(), 60, style="query"
        )
        self.assertIn("username=us%23er", url)
        self.assertNotIn("#", url)

    def test_query_slash_in_password_encoded(self):
        url = self._client(password="pass/word").build_timeshift_url(
            "5", self._ts(), 30, style="query"
        )
        self.assertIn("password=pass%2Fword", url)

    def test_query_question_mark_in_password_encoded(self):
        url = self._client(password="pass?word").build_timeshift_url(
            "5", self._ts(), 30, style="query"
        )
        self.assertIn("password=pass%3Fword", url)
        self.assertEqual(url.count("?"), 1)

    def test_query_ampersand_in_password_encoded(self):
        url = self._client(password="pa&ss").build_timeshift_url(
            "5", self._ts(), 30, style="query"
        )
        self.assertIn("password=pa%26ss", url)
        self.assertEqual(url.count("&"), 4)

    def test_query_slash_in_stream_id_encoded(self):
        url = self._client().build_timeshift_url("123/456", self._ts(), 60, style="query")
        self.assertIn("stream=123%2F456", url)

    def test_query_space_in_username_encoded(self):
        url = self._client(username="my user").build_timeshift_url(
            "1", self._ts(), 60, style="query"
        )
        self.assertIn("username=my%20user", url)


if __name__ == "__main__":
    unittest.main()
