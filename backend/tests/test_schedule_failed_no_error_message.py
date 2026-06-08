"""
Regression test for: FAILED scheduled recording shows no download error reason.

Root cause chain:
  1. _sync_schedule_status always sets schedule.status_message = None.
  2. list_schedules adds download_status/download_progress/download_output_path to the
     response but omits download.error_message.
  3. Scheduled.jsx renders {schedule.status_message && <Text>...} — nothing shown
     for a FAILED download-level error because status_message is None.

Non-technical users see a "Failed" badge with no explanation and must navigate to the
separate Downloads History page to learn why.

The fix (not implemented here): list_schedules must include download_error_message in
the response when the linked download has error_message set.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from models import ScheduledRecording, ScheduledStatus, DownloadStatus


def _make_failed_schedule(download_id=42):
    s = MagicMock(spec=ScheduledRecording)
    s.download_id = download_id
    s.status = ScheduledStatus.FAILED.value
    # _sync_schedule_status always clears this field
    s.status_message = None
    s.updated_at = None
    s.to_dict.return_value = {
        "id": 1,
        "status": ScheduledStatus.FAILED.value,
        "status_message": None,
        "download_id": download_id,
    }
    return s


def _make_failed_download(download_id=42, error_message="Provider returned an empty response. The catchup window may have expired."):
    d = MagicMock()
    d.id = download_id
    d.status = DownloadStatus.FAILED.value
    d.error_message = error_message
    d.progress = 0.0
    d.output_path = None
    return d


class ScheduleFailedErrorMessageTests(unittest.IsolatedAsyncioTestCase):
    """
    list_schedules must surface the download error reason when a schedule is FAILED.

    Without this, a non-technical user sees "Failed" with no explanation. They must
    navigate to Downloads History and match the entry by channel/time to find the cause.
    """

    async def _call_list_schedules(self, schedule, download):
        from api.schedules import list_schedules

        schedules_result = MagicMock()
        schedules_result.scalars.return_value.all.return_value = [schedule]

        downloads_result = MagicMock()
        downloads_result.scalars.return_value.all.return_value = [download]

        call_count = [0]

        async def execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return schedules_result
            return downloads_result

        session = AsyncMock()
        session.execute = execute
        session.commit = AsyncMock()

        auth = MagicMock()
        auth.is_admin = True
        auth.user_id = 1

        return await list_schedules(auth=auth, session=session)

    async def test_failed_schedule_response_includes_download_error_message(self):
        """
        GET /schedules response must include the download error_message when the linked
        download is failed, so the Scheduled History card can show why it failed.

        Currently FAILS: list_schedules adds download_status but not download_error_message.
        """
        schedule = _make_failed_schedule(download_id=42)
        download = _make_failed_download(download_id=42)

        response = await self._call_list_schedules(schedule, download)

        self.assertEqual(len(response), 1)
        item = response[0]

        error_reason = item.get("download_error_message") or item.get("status_message")
        self.assertIsNotNone(
            error_reason,
            "A failed schedule must expose the download error reason in the API response. "
            "Currently download.error_message is not included and schedule.status_message "
            "is always cleared by _sync_schedule_status, so the Scheduled History card "
            "shows 'Failed' with no explanation.",
        )
        self.assertIn(
            "Provider",
            error_reason,
            "The error reason must come from the actual download error_message.",
        )

    async def test_failed_schedule_response_has_download_error_message_key(self):
        """
        GET /schedules response must include a 'download_error_message' key (even if None)
        for consistency, so the frontend can reliably check for it.

        Currently FAILS: the key is absent entirely.
        """
        schedule = _make_failed_schedule(download_id=55)
        download = _make_failed_download(download_id=55, error_message="Connection timed out after 30s")

        response = await self._call_list_schedules(schedule, download)
        item = response[0]

        self.assertIn(
            "download_error_message",
            item,
            "list_schedules response must include 'download_error_message' key when "
            "a Download is linked, even if null, so the frontend can display it.",
        )
        self.assertEqual(
            item["download_error_message"],
            "Connection timed out after 30s",
        )

    async def test_completed_with_warnings_has_completed_download_status(self):
        """
        A completed download whose error_message is set to a warnings string
        (e.g. "Completed with warnings: comskip exited with code 1") must still
        have download_status='completed' in the API response.

        This locks in the backend contract that the frontend gate
        `download_status === 'failed'` correctly suppresses the red error alert
        for completed-with-warnings recordings.
        """
        s = MagicMock(spec=ScheduledRecording)
        s.download_id = 99
        s.status = ScheduledStatus.COMPLETED.value
        s.status_message = None
        s.updated_at = None
        s.to_dict.return_value = {
            "id": 3,
            "status": ScheduledStatus.COMPLETED.value,
            "status_message": None,
            "download_id": 99,
        }

        d = MagicMock()
        d.id = 99
        d.status = DownloadStatus.COMPLETED.value
        d.error_message = "Completed with warnings: comskip exited with code 1"
        d.progress = 1.0
        d.output_path = "/completed/show.ts"

        response = await self._call_list_schedules(s, d)
        item = response[0]

        self.assertEqual(
            item.get("download_status"),
            DownloadStatus.COMPLETED.value,
            "A completed-with-warnings download must have download_status='completed', "
            "not 'failed'. The frontend red error alert must not fire for it.",
        )
        self.assertIsNotNone(
            item.get("download_error_message"),
            "download_error_message is present even for completed-with-warnings; "
            "the frontend suppresses the red alert via the download_status gate.",
        )

    async def test_non_failed_schedule_has_no_error_message(self):
        """
        A completed schedule must not show a spurious error_message.

        This test passes even before the fix and serves as a guard against
        accidentally leaking error messages into completed schedules.
        """
        from api.schedules import list_schedules

        s = MagicMock(spec=ScheduledRecording)
        s.download_id = 7
        s.status = ScheduledStatus.COMPLETED.value
        s.status_message = None
        s.updated_at = None
        s.to_dict.return_value = {
            "id": 2,
            "status": ScheduledStatus.COMPLETED.value,
            "status_message": None,
            "download_id": 7,
        }

        d = MagicMock()
        d.id = 7
        d.status = DownloadStatus.COMPLETED.value
        d.error_message = None
        d.progress = 1.0
        d.output_path = "/completed/show.ts"

        response = await self._call_list_schedules(s, d)
        item = response[0]

        error = item.get("download_error_message")
        self.assertIsNone(
            error,
            "A completed schedule must not expose an error_message.",
        )


if __name__ == "__main__":
    unittest.main()
