import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, Download, EPGProgram, ScheduledRecording, XtreamAccount
from services.download_builder import build_download_from_program
from services.epg_service import epg_service
from services.xtream_client import XtreamClient
from api.schedules import _parse_program


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class CatchupResolutionTests(unittest.TestCase):
    def test_timeshift_url_prefers_provider_token(self):
        client = XtreamClient("https://provider.example.com", "user", "pass")
        start_time = datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc)

        url = client.build_timeshift_url("123", start_time, 60, provider_start="2026-03-30:13-00")

        self.assertIn("/60/2026-03-30:13-00/123.ts", url)

    def test_serialize_program_includes_provider_tokens(self):
        row = EPGProgram(
            id=1,
            account_id=1,
            channel_id="123",
            channel_name="Test Channel",
            xmltv_id="xmltv-1",
            epg_id="123:100:200",
            title="Test Show",
            description="Desc",
            category="News",
            start_time=datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 30, 18, 0, tzinfo=timezone.utc),
            start_timestamp=1,
            stop_timestamp=2,
            provider_start="2026-03-30:13-00",
            provider_stop="2026-03-30:14-00",
            duration_minutes=60,
            has_archive=True,
        )

        payload = epg_service.serialize_program(row)

        self.assertEqual(payload["provider_start"], "2026-03-30:13-00")
        self.assertEqual(payload["provider_stop"], "2026-03-30:14-00")

    def test_guide_offset_hours_shifts_display_only(self):
        row = EPGProgram(
            id=1,
            account_id=1,
            channel_id="123",
            channel_name="Test Channel",
            xmltv_id="xmltv-1",
            epg_id="123:100:200",
            title="Test Show",
            description="Desc",
            category="News",
            start_time=datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 30, 18, 0, tzinfo=timezone.utc),
            start_timestamp=1,
            stop_timestamp=2,
            provider_start="2026-03-30:13-00",
            provider_stop="2026-03-30:14-00",
            duration_minutes=60,
            has_archive=True,
        )
        account = XtreamAccount(
            id=1,
            name="Provider A",
            server_url="https://provider.example.com",
            username="user",
            password="",
            guide_offset_hours=4,
        )

        payload = epg_service.serialize_program(row, account)

        self.assertEqual(payload["start_time"], "2026-03-30T21:00:00+00:00")
        self.assertEqual(payload["end_time"], "2026-03-30T22:00:00+00:00")
        self.assertEqual(payload["start_timestamp"], 1)
        self.assertEqual(payload["stop_timestamp"], 2)

    def test_negative_guide_offset_shifts_display_across_day_boundary(self):
        row = EPGProgram(
            id=1,
            account_id=1,
            channel_id="123",
            channel_name="Test Channel",
            xmltv_id="xmltv-1",
            epg_id="123:100:200",
            title="Late Show",
            description="Desc",
            category="News",
            start_time=datetime(2026, 3, 30, 2, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 3, 30, 3, 0, tzinfo=timezone.utc),
            start_timestamp=1,
            stop_timestamp=2,
            provider_start=None,
            provider_stop=None,
            duration_minutes=60,
            has_archive=True,
        )
        account = XtreamAccount(
            id=1,
            name="Provider B",
            server_url="https://provider.example.com",
            username="user",
            password="",
            guide_offset_hours=-4,
        )

        payload = epg_service.serialize_program(row, account)

        self.assertEqual(payload["start_time"], "2026-03-29T22:00:00+00:00")
        self.assertEqual(payload["end_time"], "2026-03-29T23:00:00+00:00")

    def test_scheduled_recording_to_dict_includes_provider_tokens(self):
        row = ScheduledRecording(
            account_id=1,
            channel_id="123",
            channel_name="Test Channel",
            program_title="Test Show",
            program_description="Desc",
            program_start=datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc),
            program_end=datetime(2026, 3, 30, 18, 0, tzinfo=timezone.utc),
            start_timestamp=100,
            stop_timestamp=160,
            provider_start="2026-03-30:13-00",
            provider_stop="2026-03-30:14-00",
            duration_minutes=60,
        )

        payload = row.to_dict()

        self.assertEqual(payload["provider_start"], "2026-03-30:13-00")
        self.assertEqual(payload["provider_stop"], "2026-03-30:14-00")

    def test_download_to_dict_includes_program_timestamps(self):
        row = Download(
            account_id=1,
            channel_id="123",
            channel_name="Test Channel",
            program_title="Test Show",
            program_start=datetime(2026, 3, 30, 21, 0, tzinfo=timezone.utc),
            program_end=datetime(2026, 3, 30, 22, 0, tzinfo=timezone.utc),
            start_timestamp=1774904400,
            stop_timestamp=1774908000,
            duration_minutes=60,
            source_url="https://provider.example.com/timeshift/user/pass/60/2026-03-30:17-00/123.ts",
            output_path="/tmp/test.ts",
            status="pending",
        )

        payload = row.to_dict()

        self.assertEqual(payload["start_timestamp"], 1774904400)
        self.assertEqual(payload["stop_timestamp"], 1774908000)

    def test_schedule_parser_prefers_original_timestamps_over_shifted_display_times(self):
        start_time, end_time, start_ts, stop_ts, duration_minutes, _, _ = _parse_program(
            {
                "start_time": "2026-03-30T21:00:00+00:00",
                "end_time": "2026-03-30T22:00:00+00:00",
                "start_timestamp": 1774864800,
                "stop_timestamp": 1774868400,
            }
        )

        self.assertEqual(start_time, datetime.fromtimestamp(1774864800, tz=timezone.utc))
        self.assertEqual(end_time, datetime.fromtimestamp(1774868400, tz=timezone.utc))
        self.assertEqual(start_ts, 1774864800)
        self.assertEqual(stop_ts, 1774868400)
        self.assertEqual(duration_minutes, 60)

    def test_provider_token_is_normalized_from_epg_datetime_string(self):
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider A",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=4,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-03-30T14:00:00+00:00",
                    "end_time": "2026-03-30T15:00:00+00:00",
                    "start_timestamp": 1774864800,
                    "stop_timestamp": 1774868400,
                    "provider_start": "2026-03-30 05:00:00",
                },
            )
        )

        self.assertIn("/60/2026-03-30:05-00/999.ts", download.source_url)

    def test_display_offset_does_not_change_download_source_url(self):
        program = {
            "title": "Show",
            "start_time": "2026-03-30T21:00:00+00:00",
            "end_time": "2026-03-30T22:00:00+00:00",
            "start_timestamp": 1774864800,
            "stop_timestamp": 1774868400,
            "provider_start": "2026-03-30 05:00:00",
        }

        first = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider A",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=0,
                ),
                program,
            )
        )
        second = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=2,
                    name="Provider B",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=4,
                ),
                program,
            )
        )

        self.assertEqual(first.source_url, second.source_url)
        self.assertEqual(first.start_timestamp, 1774864800)
        self.assertEqual(first.stop_timestamp, 1774868400)
        self.assertEqual(second.start_timestamp, 1774864800)
        self.assertEqual(second.stop_timestamp, 1774868400)

    def test_yyyymmddhhmmss_provider_token_no_tz_is_used_as_url_start(self):
        """Xtream Codes API often returns start as YYYYMMDDHHMMSS (no dashes, no TZ).
        That string is the provider's local wall-clock time and must be used as-is
        in the timeshift URL, not replaced by the UTC fallback."""
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider A",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=2,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-04-20T19:00:00+00:00",
                    "end_time": "2026-04-20T20:15:00+00:00",
                    "start_timestamp": 1776704400,
                    "stop_timestamp": 1776708900,
                    "provider_start": "20260420190000",
                },
            )
        )

        self.assertIn("/75/2026-04-20:19-00/999.ts", download.source_url)

    def test_yyyymmddhhmmss_provider_token_with_tz_offset_uses_local_part(self):
        """XMLTV start attributes arrive as YYYYMMDDHHMMSS ±HHMM.  The datetime
        portion is the provider's local time; strip the TZ suffix and use it."""
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider B",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=2,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-04-20T17:00:00+00:00",
                    "end_time": "2026-04-20T18:15:00+00:00",
                    "start_timestamp": 1745679600,
                    "stop_timestamp": 1745684100,
                    "provider_start": "20260420190000 +0200",
                },
            )
        )

        self.assertIn("/75/2026-04-20:19-00/999.ts", download.source_url)

    def test_yyyymmddhhmmss_provider_token_compact_offset_no_space_uses_local_part(self):
        """Compact offset without whitespace (e.g. 20260420190000+0200) must strip correctly."""
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider B",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=2,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-04-20T17:00:00+00:00",
                    "end_time": "2026-04-20T18:15:00+00:00",
                    "start_timestamp": 1745679600,
                    "stop_timestamp": 1745684100,
                    "provider_start": "20260420190000+0200",
                },
            )
        )

        self.assertIn("/75/2026-04-20:19-00/999.ts", download.source_url)

    def test_yyyymmddhhmmss_provider_token_z_suffix_uses_local_part(self):
        """Z-suffix variant (e.g. 20260420190000Z) strips correctly."""
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider B",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=0,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-04-20T19:00:00+00:00",
                    "end_time": "2026-04-20T20:15:00+00:00",
                    "start_timestamp": 1745686800,
                    "stop_timestamp": 1745691300,
                    "provider_start": "20260420190000Z",
                },
            )
        )

        self.assertIn("/75/2026-04-20:19-00/999.ts", download.source_url)

    def test_yyyymmddhhmmss_provider_token_colon_offset_uses_local_part(self):
        """Colon offset variant (e.g. 20260420190000+02:00) strips correctly."""
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider B",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=2,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-04-20T17:00:00+00:00",
                    "end_time": "2026-04-20T18:15:00+00:00",
                    "start_timestamp": 1745679600,
                    "stop_timestamp": 1745684100,
                    "provider_start": "20260420190000+02:00",
                },
            )
        )

        self.assertIn("/75/2026-04-20:19-00/999.ts", download.source_url)

    def test_invalid_provider_token_falls_back_to_generated_start_without_account_offset(self):
        download = asyncio.run(
            self._build_download(
                XtreamAccount(
                    id=1,
                    name="Provider A",
                    server_url="https://provider.example.com",
                    username="user",
                    password="",
                    guide_offset_hours=4,
                ),
                {
                    "title": "Show",
                    "start_time": "2026-03-30T21:00:00+00:00",
                    "end_time": "2026-03-30T22:00:00+00:00",
                    "start_timestamp": 1774864800,
                    "stop_timestamp": 1774868400,
                    "provider_start": "invalid-token",
                },
            )
        )

        self.assertIn("/60/2026-03-30:10-00/999.ts", download.source_url)

    async def _build_download(self, account, program, pre_padding=0, post_padding=0):
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _ScalarResult(account),
                _ScalarResult(AppSettings(download_folder="/tmp")),
            ]
        )

        with patch("services.download_builder.resolve_account_password_with_migration", new=AsyncMock(return_value="pass")):
            with patch("services.download_builder.file_namer.generate_filename", return_value="test.ts"):
                with patch("services.download_builder.epg_service.detect_program_type", return_value="other"):
                    return await build_download_from_program(
                        session,
                        account_id=account.id,
                        channel_id="999",
                        channel_name="Test Channel",
                        program=program,
                        pre_padding_minutes=pre_padding,
                        post_padding_minutes=post_padding,
                    )


if __name__ == "__main__":
    unittest.main()
