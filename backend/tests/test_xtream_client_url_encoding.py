import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.xtream_client import XtreamClient


def _qs(url):
    return parse_qs(urlparse(url).query, keep_blank_values=True)


class BuildApiUrlEncodingTests(unittest.TestCase):
    def _client(self, username="user", password="pass"):
        return XtreamClient("http://provider.test:8080", username, password)

    def test_plus_in_password_encoded(self):
        url = self._client(password="abc+def")._build_api_url("get_live_categories")
        self.assertEqual(_qs(url)["password"], ["abc+def"])

    def test_ampersand_in_password_encoded(self):
        url = self._client(password="p&ssword")._build_api_url("get_live_categories")
        self.assertEqual(_qs(url)["password"], ["p&ssword"])

    def test_equals_in_password_encoded(self):
        url = self._client(password="base64==")._build_api_url("get_live_categories")
        self.assertEqual(_qs(url)["password"], ["base64=="])

    def test_percent_in_password_not_double_decoded(self):
        url = self._client(password="p%40ss")._build_api_url("get_live_categories")
        self.assertEqual(_qs(url)["password"], ["p%40ss"])

    def test_special_chars_in_username_encoded(self):
        url = self._client(username="user+name")._build_api_url("get_live_categories")
        self.assertEqual(_qs(url)["username"], ["user+name"])

    def test_normal_credentials_and_extra_params_pass_through(self):
        url = self._client(username="myuser", password="secret123")._build_api_url(
            "get_live_streams", category_id="5"
        )
        qs = _qs(url)
        self.assertEqual(qs["username"], ["myuser"])
        self.assertEqual(qs["password"], ["secret123"])
        self.assertEqual(qs["action"], ["get_live_streams"])
        self.assertEqual(qs["category_id"], ["5"])

    def test_none_extra_param_omitted(self):
        url = self._client()._build_api_url("get_live_streams", category_id=None)
        self.assertNotIn("category_id", _qs(url))


if __name__ == "__main__":
    unittest.main()
