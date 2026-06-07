import sys
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


class PathUrlEncodingTests(unittest.TestCase):
    def _client(self, username="user", password="pass"):
        return XtreamClient("http://provider.test:8080", username, password)

    def _ts(self):
        return datetime(2024, 1, 15, 20, 30)

    # --- hash fragment delimiter ---

    def test_hash_in_password_encoded_timeshift(self):
        url = self._client(password="p@ss#word").build_timeshift_url("123", self._ts(), 60)
        self.assertIn("%23", url)
        self.assertNotIn("#", url.split("//", 1)[1])

    def test_hash_in_username_encoded_timeshift(self):
        url = self._client(username="us#er").build_timeshift_url("123", self._ts(), 60)
        self.assertIn("%23", url)
        self.assertNotIn("#", url.split("//", 1)[1])

    def test_hash_in_password_encoded_stream(self):
        url = self._client(password="p#ss").build_stream_url("99")
        self.assertIn("%23", url)
        self.assertNotIn("#", url.split("//", 1)[1])

    def test_hash_in_password_encoded_vod(self):
        url = self._client(password="p#ss").build_vod_url("42")
        self.assertIn("%23", url)
        self.assertNotIn("#", url.split("//", 1)[1])

    def test_hash_in_password_encoded_series(self):
        url = self._client(password="p#ss").build_series_url("7", "mkv")
        self.assertIn("%23", url)
        self.assertNotIn("#", url.split("//", 1)[1])

    # --- other problematic chars ---

    def test_question_mark_in_password_encoded(self):
        url = self._client(password="pass?word").build_stream_url("1")
        self.assertIn("%3F", url)
        self.assertNotIn("?", url.split("//", 1)[1])

    def test_slash_in_password_encoded(self):
        url = self._client(password="pass/word").build_timeshift_url("5", self._ts(), 30)
        self.assertIn("%2F", url)

    def test_space_in_username_encoded(self):
        url = self._client(username="my user").build_vod_url("10")
        self.assertIn("%20", url)

    # --- slash in stream/vod/episode ID ---

    def test_slash_in_stream_id_encoded(self):
        url = self._client().build_stream_url("123/456")
        self.assertIn("%2F", url)
        # encoded id sits between the two surrounding slashes
        self.assertIn("/123%2F456.ts", url)

    def test_slash_in_timeshift_stream_id_encoded(self):
        url = self._client().build_timeshift_url("123/456", self._ts(), 60)
        self.assertIn("%2F", url)
        self.assertIn("/123%2F456.ts", url)

    def test_slash_in_vod_id_encoded(self):
        url = self._client().build_vod_url("123/456")
        self.assertIn("%2F", url)
        self.assertIn("/123%2F456.mp4", url)

    def test_slash_in_series_id_encoded(self):
        url = self._client().build_series_url("123/456", "mkv")
        self.assertIn("%2F", url)
        self.assertIn("/123%2F456.mkv", url)

    # --- normal credentials pass through intact ---

    def test_normal_credentials_unchanged(self):
        url = self._client(username="myuser", password="secret123").build_stream_url("55")
        self.assertIn("/myuser/", url)
        self.assertIn("/secret123/", url)

    def test_timeshift_date_preserved(self):
        url = self._client().build_timeshift_url("77", self._ts(), 90)
        self.assertIn("2024-01-15:20-30", url)

    def test_timeshift_provider_start_preserved(self):
        url = self._client().build_timeshift_url("77", self._ts(), 90, provider_start="2024-01-15:20-30")
        self.assertIn("2024-01-15:20-30", url)


if __name__ == "__main__":
    unittest.main()
