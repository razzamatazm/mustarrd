"""
Tests for GET /api/downloads/upcoming.

Returns a list of ScheduledRecording rows with status SCHEDULED or
PAUSED_LOW_SPACE, ordered by program_start ascending.

A non-admin user sees only their own scheduled recordings.
"""
import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import AppSettings, ScheduledStatus


def _make_recording(rec_id, title, channel, program_start, status="scheduled", user_id=1):
    rec = MagicMock()
    rec.id = rec_id
    rec.program_title = title
    rec.channel_name = channel
    rec.program_start = program_start
    rec.program_end = program_start
    rec.stop_timestamp = None
    rec.post_padding_minutes = 0
    rec.status = status
    rec.to_dict.return_value = {
        "id": rec_id,
        "program_title": title,
        "channel_name": channel,
        "program_start": program_start.isoformat(),
        "status": status,
        "requested_by_user_id": user_id,
    }
    return rec


class UpcomingRecordingsTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _make_admin_auth(self):
        auth = MagicMock()
        auth.is_admin = True
        auth.user_id = 1
        return auth

    def _make_user_auth(self, user_id=2):
        auth = MagicMock()
        auth.is_admin = False
        auth.user_id = user_id
        return auth

    def _make_session(self, recordings):
        scalars_result = MagicMock()
        scalars_result.all = MagicMock(return_value=recordings)
        execute_result = MagicMock()
        execute_result.scalars = MagicMock(return_value=scalars_result)
        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = AppSettings(
            scheduled_download_delay_minutes=5
        )
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[execute_result, settings_result])
        return session

    def _capture_session(self, recordings=None):
        captured = {}
        recordings = recordings or []
        scalars_result = MagicMock()
        scalars_result.all = MagicMock(return_value=recordings)
        execute_result = MagicMock()
        execute_result.scalars = MagicMock(return_value=scalars_result)
        settings_result = MagicMock()
        settings_result.scalar_one_or_none.return_value = AppSettings(
            scheduled_download_delay_minutes=5
        )

        call_count = 0

        async def capture_execute(query, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                captured["stmt"] = query
                return execute_result
            return settings_result

        session = AsyncMock()
        session.execute = capture_execute
        return session, captured

    def test_returns_list_for_admin(self):
        from api.downloads import get_upcoming_recordings
        t1 = datetime(2026, 6, 10, 20, 0)
        t2 = datetime(2026, 6, 11, 21, 0)
        recs = [
            _make_recording(1, "Show A", "Channel 1", t1),
            _make_recording(2, "Show B", "Channel 2", t2),
        ]
        session = self._make_session(recs)
        auth = self._make_admin_auth()
        result = self._run(get_upcoming_recordings(auth=auth, session=session))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["program_title"], "Show A")
        self.assertEqual(result[1]["program_title"], "Show B")

    def test_returns_empty_list_when_none_scheduled(self):
        from api.downloads import get_upcoming_recordings
        session = self._make_session([])
        auth = self._make_admin_auth()
        result = self._run(get_upcoming_recordings(auth=auth, session=session))
        self.assertEqual(result, [])

    def test_query_filters_scheduled_and_paused_low_space(self):
        from api.downloads import get_upcoming_recordings
        session, captured = self._capture_session()
        auth = self._make_admin_auth()
        self._run(get_upcoming_recordings(auth=auth, session=session))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn(f"'{ScheduledStatus.SCHEDULED.value}'", compiled)
        self.assertIn(f"'{ScheduledStatus.PAUSED_LOW_SPACE.value}'", compiled)

    def test_query_orders_by_program_start_ascending(self):
        from api.downloads import get_upcoming_recordings
        session, captured = self._capture_session()
        auth = self._make_admin_auth()
        self._run(get_upcoming_recordings(auth=auth, session=session))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("program_start", compiled.lower())
        self.assertIn("ASC", compiled)

    def test_non_admin_scoped_to_user(self):
        from api.downloads import get_upcoming_recordings
        session, captured = self._capture_session()
        auth = self._make_user_auth(user_id=42)
        self._run(get_upcoming_recordings(auth=auth, session=session))
        stmt = captured.get("stmt")
        self.assertIsNotNone(stmt)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("42", compiled, "user_id filter must appear for non-admin")

    def test_paused_low_space_recording_included(self):
        from api.downloads import get_upcoming_recordings
        t = datetime(2026, 6, 12, 9, 0)
        recs = [_make_recording(3, "Paused Show", "Channel 3", t, status="paused_low_space")]
        session = self._make_session(recs)
        auth = self._make_admin_auth()
        result = self._run(get_upcoming_recordings(auth=auth, session=session))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "paused_low_space")


if __name__ == "__main__":
    unittest.main()
