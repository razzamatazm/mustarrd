import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.channels import _channel_has_tv_archive
from services.epg_service import EPGService


class TvArchiveFilterTests(unittest.TestCase):
    """_channel_has_tv_archive() (the Browse filter in api/channels.py) must accept int 1 and string "1"."""

    def test_int_1_included(self):
        self.assertTrue(_channel_has_tv_archive({"tv_archive": 1}))

    def test_string_1_included(self):
        self.assertTrue(_channel_has_tv_archive({"tv_archive": "1"}))

    def test_int_0_excluded(self):
        self.assertFalse(_channel_has_tv_archive({"tv_archive": 0}))

    def test_string_0_excluded(self):
        self.assertFalse(_channel_has_tv_archive({"tv_archive": "0"}))

    def test_missing_field_excluded(self):
        self.assertFalse(_channel_has_tv_archive({}))

    def test_none_excluded(self):
        self.assertFalse(_channel_has_tv_archive({"tv_archive": None}))

    def test_mixed_list_returns_only_archive_channels(self):
        channels = [
            {"stream_id": "10", "tv_archive": "1"},
            {"stream_id": "11", "tv_archive": 1},
            {"stream_id": "12", "tv_archive": 0},
            {"stream_id": "13", "tv_archive": "0"},
            {"stream_id": "14"},
        ]
        result = [ch for ch in channels if _channel_has_tv_archive(ch)]
        self.assertEqual(len(result), 2)
        self.assertEqual({ch["stream_id"] for ch in result}, {"10", "11"})


class HasArchiveCoercionTests(unittest.TestCase):
    """_process_epg_entry() must set has_archive=True for both int 1 and string "1"."""

    def setUp(self):
        self.svc = EPGService()

    def _entry(self, has_archive_value):
        return {
            "has_archive": has_archive_value,
            "start_timestamp": 1748995200,
            "stop_timestamp": 1748998800,
            "title": "VGVzdA==",  # base64 "Test"
            "channel_id": "ch1",
        }

    def test_int_1_sets_true(self):
        result = self.svc._process_epg_entry(self._entry(1))
        self.assertTrue(result["has_archive"])

    def test_string_1_sets_true(self):
        result = self.svc._process_epg_entry(self._entry("1"))
        self.assertTrue(result["has_archive"])

    def test_int_0_sets_false(self):
        result = self.svc._process_epg_entry(self._entry(0))
        self.assertFalse(result["has_archive"])

    def test_string_0_sets_false(self):
        result = self.svc._process_epg_entry(self._entry("0"))
        self.assertFalse(result["has_archive"])

    def test_missing_field_sets_false(self):
        entry = dict(self._entry(0))
        del entry["has_archive"]
        result = self.svc._process_epg_entry(entry)
        self.assertFalse(result["has_archive"])

    def test_none_sets_false(self):
        result = self.svc._process_epg_entry(self._entry(None))
        self.assertFalse(result["has_archive"])
