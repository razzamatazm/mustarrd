"""
Tests for time slot recording: a hand-picked channel + start + end, with no EPG
program behind it.

Because the times are typed rather than taken from the provider's guide, the
bounds a guide-picked programme gets for free have to be enforced here: the
channel's catchup depth backwards, 14 days forwards, and a 6 hour cap on the
range itself. The other thing with no guide equivalent is the provider's
wall-clock start token, which has to be derived from the account's guide offset
or the provider serves back the wrong hour.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import Download, DownloadStatus, ScheduledRecording, ScheduledStatus, User, XtreamAccount
from api.time_slots import (
    DEFAULT_ARCHIVE_DAYS,
    MAX_FUTURE_DAYS,
    MAX_TIME_SLOT_MINUTES,
    TimeSlotCreate,
    build_time_slot_program,
    create_time_slot,
    validate_time_slot,
)


NOW = 1_700_000_000


def _auth(user_id=1, is_admin=True):
    auth = MagicMock()
    auth.user_id = user_id
    auth.is_admin = is_admin
    auth.provider = "admin_local" if is_admin else "local"
    return auth


class ValidateTimeSlotTests(unittest.TestCase):
    """The bounds a typed time range needs and a guide-picked programme does not."""

    def test_accepts_an_ordinary_past_range(self):
        start = NOW - 3 * 3600
        validate_time_slot(start, start + 7200, archive_days=7, now_ts=NOW)

    def test_rejects_end_before_start(self):
        start = NOW - 3600
        with self.assertRaises(ValueError) as ctx:
            validate_time_slot(start, start - 60, archive_days=7, now_ts=NOW)
        self.assertIn("after the start time", str(ctx.exception))

    def test_rejects_end_equal_to_start(self):
        with self.assertRaises(ValueError):
            validate_time_slot(NOW - 3600, NOW - 3600, archive_days=7, now_ts=NOW)

    def test_rejects_range_longer_than_the_cap(self):
        start = NOW - 20 * 3600
        too_long = start + (MAX_TIME_SLOT_MINUTES + 1) * 60
        with self.assertRaises(ValueError) as ctx:
            validate_time_slot(start, too_long, archive_days=30, now_ts=NOW)
        self.assertIn("at most 6 hours", str(ctx.exception))

    def test_accepts_a_range_exactly_at_the_cap(self):
        start = NOW - 20 * 3600
        validate_time_slot(
            start, start + MAX_TIME_SLOT_MINUTES * 60, archive_days=30, now_ts=NOW
        )

    def test_rejects_a_start_older_than_the_channel_archive(self):
        start = NOW - 8 * 86400
        with self.assertRaises(ValueError) as ctx:
            validate_time_slot(start, start + 3600, archive_days=7, now_ts=NOW)
        message = str(ctx.exception)
        self.assertIn("catchup window", message)
        self.assertIn("7 days", message)

    def test_archive_bound_follows_the_channel_not_a_constant(self):
        start = NOW - 8 * 86400
        # Same range, deeper archive: now fine.
        validate_time_slot(start, start + 3600, archive_days=14, now_ts=NOW)

    def test_unknown_archive_depth_falls_back_to_the_default(self):
        start = NOW - (DEFAULT_ARCHIVE_DAYS + 1) * 86400
        with self.assertRaises(ValueError) as ctx:
            validate_time_slot(start, start + 3600, archive_days=0, now_ts=NOW)
        self.assertIn(f"{DEFAULT_ARCHIVE_DAYS} days", str(ctx.exception))

    def test_rejects_a_start_too_far_in_the_future(self):
        start = NOW + (MAX_FUTURE_DAYS + 1) * 86400
        with self.assertRaises(ValueError) as ctx:
            validate_time_slot(start, start + 3600, archive_days=7, now_ts=NOW)
        self.assertIn("14 days in the future", str(ctx.exception))

    def test_accepts_a_start_just_inside_the_future_bound(self):
        start = NOW + (MAX_FUTURE_DAYS * 86400) - 3600
        validate_time_slot(start, start + 3600, archive_days=7, now_ts=NOW)


class TimeSlotProgramTests(unittest.TestCase):
    """The provider start token is the whole timezone story for a time slot."""

    def test_carries_the_title_and_timestamps_through(self):
        program = build_time_slot_program("Wimbledon QF", NOW, NOW + 3600)
        self.assertEqual(program["title"], "Wimbledon QF")
        self.assertEqual(program["start_timestamp"], NOW)
        self.assertEqual(program["stop_timestamp"], NOW + 3600)

    def test_provider_start_is_utc_when_there_is_no_guide_offset(self):
        start = int(datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc).timestamp())
        program = build_time_slot_program("Tennis", start, start + 3600)
        self.assertEqual(program["provider_start"], "2026-08-15:14-00")

    def test_provider_start_is_shifted_by_a_positive_guide_offset(self):
        # The user typed 2pm against a guide running +2, so the provider's own
        # clock says 4pm and that is what the timeshift URL must ask for.
        start = int(datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc).timestamp())
        program = build_time_slot_program("Tennis", start, start + 3600, guide_offset_hours=2)
        self.assertEqual(program["provider_start"], "2026-08-15:16-00")

    def test_provider_start_is_shifted_by_a_negative_guide_offset(self):
        start = int(datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc).timestamp())
        program = build_time_slot_program("Tennis", start, start + 3600, guide_offset_hours=-5)
        self.assertEqual(program["provider_start"], "2026-08-15:09-00")

    def test_negative_offset_can_cross_a_date_boundary(self):
        start = int(datetime(2026, 8, 15, 2, 0, tzinfo=timezone.utc).timestamp())
        program = build_time_slot_program("Overnight", start, start + 3600, guide_offset_hours=-5)
        self.assertEqual(program["provider_start"], "2026-08-14:21-00")


class CreateTimeSlotTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                id=1, name="acc", server_url="http://provider.test",
                username="u", password="p", guide_offset_hours=0,
            ))
            session.add(User(id=1, role="admin", username="admin"))
            session.add(User(id=2, role="download_only", username="alice"))
            await session.commit()

        self.archive_patch = patch(
            "api.time_slots.epg_service.get_channel_archive_days",
            new=AsyncMock(return_value=7),
        )
        self.archive_patch.start()
        self.addCleanup(self.archive_patch.stop)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _payload(self, **overrides):
        start = int(datetime.now(timezone.utc).timestamp()) + 3600
        payload = dict(
            account_id=1,
            channel_id="ch1",
            channel_name="ESPN",
            title="Wimbledon QF",
            start_timestamp=start,
            stop_timestamp=start + 7200,
        )
        payload.update(overrides)
        return TimeSlotCreate(**payload)

    async def test_a_future_range_becomes_a_scheduled_recording(self):
        async with self.session_factory() as session:
            result = await create_time_slot(self._payload(), _auth(), session)

        self.assertEqual(result["kind"], "schedule")
        self.assertEqual(result["record"]["program_title"], "Wimbledon QF")
        # A time slot records exactly the range entered.
        self.assertEqual(result["record"]["pre_padding_minutes"], 0)
        self.assertEqual(result["record"]["post_padding_minutes"], 0)

        async with self.session_factory() as session:
            rows = (await session.execute(select(ScheduledRecording))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, ScheduledStatus.SCHEDULED.value)
        # No EPG program behind it.
        self.assertIsNone(rows[0].epg_id)
        self.assertIsNone(rows[0].program_id)

    async def test_a_past_range_downloads_immediately(self):
        start = int(datetime.now(timezone.utc).timestamp()) - 3 * 3600
        payload = self._payload(start_timestamp=start, stop_timestamp=start + 7200)

        queued = MagicMock()
        queued.to_dict.return_value = {"id": 7, "status": "pending"}

        with patch("api.time_slots.check_disk_space", new=AsyncMock()), \
             patch("api.time_slots.download_manager.queue_download",
                   new=AsyncMock(return_value=queued)), \
             patch("api.time_slots.build_download_from_program",
                   new=AsyncMock()) as build:
            build.return_value = MagicMock(
                account_id=1, channel_id="ch1",
                start_timestamp=start, stop_timestamp=start + 7200,
            )
            async with self.session_factory() as session:
                result = await create_time_slot(payload, _auth(), session)

        self.assertEqual(result["kind"], "download")
        self.assertEqual(result["record"]["id"], 7)
        # Padding is never applied to a time slot.
        self.assertEqual(build.await_args.kwargs["pre_padding_minutes"], 0)
        self.assertEqual(build.await_args.kwargs["post_padding_minutes"], 0)
        self.assertEqual(build.await_args.kwargs["program"]["title"], "Wimbledon QF")

    async def test_disk_space_is_checked_before_queueing_a_past_range(self):
        start = int(datetime.now(timezone.utc).timestamp()) - 3 * 3600
        payload = self._payload(start_timestamp=start, stop_timestamp=start + 7200)

        built = MagicMock(
            account_id=1, channel_id="ch1",
            start_timestamp=start, stop_timestamp=start + 7200,
        )
        with patch("api.time_slots.check_disk_space",
                   new=AsyncMock(side_effect=HTTPException(status_code=507, detail="full"))), \
             patch("api.time_slots.download_manager.queue_download",
                   new=AsyncMock()) as queue, \
             patch("api.time_slots.build_download_from_program",
                   new=AsyncMock(return_value=built)):
            async with self.session_factory() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await create_time_slot(payload, _auth(), session)

        self.assertEqual(ctx.exception.status_code, 507)
        queue.assert_not_awaited()

    async def test_a_range_outside_the_catchup_window_is_rejected(self):
        start = int(datetime.now(timezone.utc).timestamp()) - 9 * 86400
        payload = self._payload(start_timestamp=start, stop_timestamp=start + 3600)

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await create_time_slot(payload, _auth(), session)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("7 days of catchup", ctx.exception.detail)

    async def test_a_range_longer_than_the_cap_is_rejected(self):
        start = int(datetime.now(timezone.utc).timestamp()) + 3600
        payload = self._payload(
            start_timestamp=start,
            stop_timestamp=start + (MAX_TIME_SLOT_MINUTES + 30) * 60,
        )

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await create_time_slot(payload, _auth(), session)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("6 hours", ctx.exception.detail)

    async def test_a_channel_without_catchup_is_rejected_with_the_reason(self):
        from services.epg_service import NoCatchupSupportError

        with patch("api.time_slots.epg_service.get_channel_archive_days",
                   new=AsyncMock(side_effect=NoCatchupSupportError("'ESPN' does not support catchup recording."))):
            async with self.session_factory() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await create_time_slot(self._payload(), _auth(), session)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("does not support catchup", ctx.exception.detail)

    async def test_a_blank_title_is_rejected(self):
        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await create_time_slot(self._payload(title="   "), _auth(), session)

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_an_unknown_account_is_rejected(self):
        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await create_time_slot(self._payload(account_id=999), _auth(), session)

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_an_overlapping_range_warns_once_then_succeeds_when_confirmed(self):
        start = int(datetime.now(timezone.utc).timestamp()) + 3600
        async with self.session_factory() as session:
            session.add(ScheduledRecording(
                account_id=1, channel_id="ch1", channel_name="ESPN",
                program_title="Tennis, wide", program_start=datetime.utcnow(),
                program_end=datetime.utcnow(), duration_minutes=180,
                start_timestamp=start, stop_timestamp=start + 10800,
                status=ScheduledStatus.SCHEDULED.value, requested_by_user_id=1,
            ))
            await session.commit()

        # Overlaps the existing schedule but is not identical to it.
        payload = self._payload(start_timestamp=start + 3600, stop_timestamp=start + 7200)

        async with self.session_factory() as session:
            with self.assertRaises(HTTPException) as ctx:
                await create_time_slot(payload, _auth(), session)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "overlap")
        self.assertIn("Tennis, wide", ctx.exception.detail["message"])

        confirmed = self._payload(
            start_timestamp=start + 3600,
            stop_timestamp=start + 7200,
            confirm_overlap=True,
        )
        async with self.session_factory() as session:
            result = await create_time_slot(confirmed, _auth(), session)
        self.assertEqual(result["kind"], "schedule")

    async def test_a_non_overlapping_range_on_the_same_channel_is_untouched(self):
        start = int(datetime.now(timezone.utc).timestamp()) + 3600
        async with self.session_factory() as session:
            session.add(ScheduledRecording(
                account_id=1, channel_id="ch1", channel_name="ESPN",
                program_title="Earlier", program_start=datetime.utcnow(),
                program_end=datetime.utcnow(), duration_minutes=60,
                start_timestamp=start, stop_timestamp=start + 3600,
                status=ScheduledStatus.SCHEDULED.value, requested_by_user_id=1,
            ))
            await session.commit()

        # Starts exactly where the other stops.
        payload = self._payload(start_timestamp=start + 3600, stop_timestamp=start + 7200)
        async with self.session_factory() as session:
            result = await create_time_slot(payload, _auth(), session)
        self.assertEqual(result["kind"], "schedule")

    async def test_a_download_only_user_can_create_a_time_slot(self):
        async with self.session_factory() as session:
            result = await create_time_slot(
                self._payload(), _auth(user_id=2, is_admin=False), session
            )

        self.assertEqual(result["kind"], "schedule")
        self.assertEqual(result["record"]["requested_by_user_id"], 2)

    async def test_a_cancelled_recording_does_not_count_as_an_overlap(self):
        start = int(datetime.now(timezone.utc).timestamp()) + 3600
        async with self.session_factory() as session:
            session.add(ScheduledRecording(
                account_id=1, channel_id="ch1", channel_name="ESPN",
                program_title="Cancelled", program_start=datetime.utcnow(),
                program_end=datetime.utcnow(), duration_minutes=180,
                start_timestamp=start, stop_timestamp=start + 10800,
                status=ScheduledStatus.CANCELLED.value, requested_by_user_id=1,
            ))
            await session.commit()

        payload = self._payload(start_timestamp=start + 3600, stop_timestamp=start + 7200)
        async with self.session_factory() as session:
            result = await create_time_slot(payload, _auth(), session)
        self.assertEqual(result["kind"], "schedule")


if __name__ == "__main__":
    unittest.main()
