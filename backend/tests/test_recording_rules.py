import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.recording_rules import (
    RecordingRuleCreate,
    RecordingRuleUpdate,
    create_recording_rule,
    delete_recording_rule,
    update_recording_rule,
)
from auth import AuthContext
from database import Base
from fastapi import HTTPException
from api.accounts import delete_account
from api.admin_users import delete_user
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    EPGProgram,
    RecordingRule,
    ScheduledRecording,
    ScheduledStatus,
    User,
    XtreamAccount,
)
from services.recording_rule_service import (
    BACKFILL_HOURS,
    MAX_PENDING_SCHEDULES_PER_RULE,
    MAX_TITLE_MATCH_LENGTH,
    recording_rule_service,
)


class RecordingRuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.recording_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.recording_dir.cleanup)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self.session_factory() as session:
            session.add(
                XtreamAccount(
                    id=1,
                    name="Provider",
                    server_url="http://provider.test",
                    username="user",
                    password="password",
                )
            )
            session.add(User(id=1, role="admin", username="admin"))
            session.add(
                AppSettings(
                    download_folder=self.recording_dir.name,
                    completed_folder=self.recording_dir.name,
                )
            )
            await session.commit()

        self.auth = AuthContext(
            authenticated=True,
            role="admin",
            provider="admin_local",
            user_id=1,
            is_admin=True,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _add_program(
        self,
        *,
        channel_id="101",
        channel_name="Example TV",
        title="The Example Show",
        hours_from_now=2,
        suffix="1",
    ):
        start = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
        end = start + timedelta(hours=1)
        async with self.session_factory() as session:
            session.add(
                EPGProgram(
                    account_id=1,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    epg_id=f"epg-{suffix}",
                    title=title,
                    start_time=start,
                    end_time=end,
                    start_timestamp=int(start.timestamp()),
                    stop_timestamp=int(end.timestamp()),
                    duration_minutes=60,
                    has_archive=True,
                )
            )
            await session.commit()

    async def _create_rule(
        self,
        *,
        channel_id="101",
        channel_name="Example TV",
        title_match="The Example Show",
        enabled=True,
        match_mode="exact",
        days_of_week=None,
    ):
        async with self.session_factory() as session:
            return await create_recording_rule(
                data=RecordingRuleCreate(
                    account_id=1,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    title_match=title_match,
                    enabled=enabled,
                    match_mode=match_mode,
                    days_of_week=days_of_week,
                ),
                auth=self.auth,
                session=session,
            )

    async def _set_offsets(self, *, guide_offset_hours=0, epg_offset_minutes=0):
        async with self.session_factory() as session:
            account = (
                await session.execute(
                    select(XtreamAccount).where(XtreamAccount.id == 1)
                )
            ).scalar_one()
            account.guide_offset_hours = guide_offset_hours
            settings = (await session.execute(select(AppSettings))).scalar_one()
            settings.epg_offset_minutes = epg_offset_minutes
            await session.commit()

    async def _count(self, model):
        async with self.session_factory() as session:
            return (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()

    async def test_creating_rule_matches_future_epg_item(self):
        await self._add_program()

        result = await self._create_rule()

        self.assertEqual(result["title_match"], "The Example Show")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(await self._count(RecordingRule), 1)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_rule_schedules_a_currently_airing_programme(self):
        # Airing started 30 minutes ago and runs for another 30.
        await self._add_program(hours_from_now=-0.5, suffix="on-air-now")

        result = await self._create_rule()

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_rule_ignores_programme_past_the_archive_window(self):
        async with self.session_factory() as session:
            account = (
                await session.execute(
                    select(XtreamAccount).where(XtreamAccount.id == 1)
                )
            ).scalar_one()
            account.catchup_max_archive_days = 7
            await session.commit()
        # Finished 10 days ago: outside a 7-day archive window.
        await self._add_program(hours_from_now=-24 * 10, suffix="too-old")

        result = await self._create_rule()

        self.assertEqual(result["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def _add_program_at(self, start_utc, *, suffix):
        end = start_utc + timedelta(hours=1)
        async with self.session_factory() as session:
            session.add(
                EPGProgram(
                    account_id=1,
                    channel_id="101",
                    channel_name="Example TV",
                    epg_id=f"epg-{suffix}",
                    title="The Example Show",
                    start_time=start_utc,
                    end_time=end,
                    start_timestamp=int(start_utc.timestamp()),
                    stop_timestamp=int(end.timestamp()),
                    duration_minutes=60,
                    has_archive=True,
                )
            )
            await session.commit()

    async def test_weekday_filter_uses_the_global_epg_offset(self):
        # Airing at 01:00 UTC tomorrow; a -2h global offset puts its guide
        # wall clock on the previous calendar day, so the weekday differs.
        start_utc = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).replace(hour=1, minute=0, second=0, microsecond=0)
        local_weekday = (start_utc - timedelta(hours=2)).weekday()
        utc_weekday = start_utc.weekday()
        self.assertNotEqual(local_weekday, utc_weekday)
        await self._set_offsets(epg_offset_minutes=-120)

        await self._add_program_at(start_utc, suffix="weekday-utc-day")
        wrong_day = await self._create_rule(days_of_week=[utc_weekday])
        self.assertEqual(wrong_day["scheduled_count"], 0)

        right_day = await self._create_rule(days_of_week=[local_weekday])
        self.assertEqual(right_day["scheduled_count"], 1)

    async def test_wrong_channel_does_not_match(self):
        await self._add_program(channel_id="202", suffix="wrong-channel")

        result = await self._create_rule(channel_id="101")

        self.assertEqual(result["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_different_title_does_not_match(self):
        await self._add_program(title="A Different Show", suffix="wrong-title")

        result = await self._create_rule(title_match="The Example Show")

        self.assertEqual(result["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_title_match_is_case_insensitive_and_collapses_whitespace(self):
        await self._add_program(title="  THE   Example\tShow  ", suffix="normalized")

        result = await self._create_rule(title_match="the example show")

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_contains_title_match(self):
        await self._add_program(title="The Evening News at Nine", suffix="contains")

        result = await self._create_rule(
            title_match="evening   news", match_mode="contains"
        )

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["match_mode"], "contains")

    async def test_case_insensitive_regex_title_match(self):
        await self._add_program(title="News at 9", suffix="regex")

        result = await self._create_rule(
            title_match=r"^news at (6|9)$", match_mode="regex"
        )

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["match_mode"], "regex")

    async def test_invalid_regex_is_rejected(self):
        with self.assertRaises(ValidationError):
            RecordingRuleCreate(
                account_id=1,
                channel_id="101",
                channel_name="Example TV",
                title_match="[unterminated",
                match_mode="regex",
            )

    async def test_updating_rule_re_evaluates_current_future_epg(self):
        await self._add_program(title="The Evening News", suffix="edited")
        rule = await self._create_rule(title_match="No Match")

        async with self.session_factory() as session:
            result = await update_recording_rule(
                rule_id=rule["id"],
                data=RecordingRuleUpdate(
                    title_match="evening news",
                    match_mode="contains",
                    pre_padding_minutes=2,
                    post_padding_minutes=8,
                ),
                auth=self.auth,
                session=session,
            )

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["match_mode"], "contains")
        async with self.session_factory() as session:
            schedule = (
                await session.execute(select(ScheduledRecording))
            ).scalar_one()
            self.assertEqual(schedule.pre_padding_minutes, 2)
            self.assertEqual(schedule.post_padding_minutes, 8)

    async def test_repeated_evaluation_does_not_duplicate_schedule(self):
        await self._add_program(suffix="repeat")
        await self._create_rule()

        async with self.session_factory() as session:
            first_repeat = await recording_rule_service.evaluate(session, account_id=1)
            second_repeat = await recording_rule_service.evaluate(session, account_id=1)

        self.assertEqual(first_repeat, 0)
        self.assertEqual(second_repeat, 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_cancelled_rule_airing_does_not_block_new_future_airings(self):
        await self._add_program(suffix="cancelled-first")
        await self._create_rule()
        async with self.session_factory() as session:
            schedule = (
                await session.execute(select(ScheduledRecording))
            ).scalar_one()
            schedule.status = "cancelled"
            await session.commit()

        await self._add_program(hours_from_now=4, suffix="new-airing")
        async with self.session_factory() as session:
            created = await recording_rule_service.evaluate(session, account_id=1)

        self.assertEqual(created, 1)
        self.assertEqual(await self._count(ScheduledRecording), 2)

    async def test_disabled_rule_does_nothing(self):
        await self._add_program(suffix="disabled")
        result = await self._create_rule(enabled=False)

        async with self.session_factory() as session:
            created = await recording_rule_service.evaluate(session, account_id=1)

        self.assertEqual(result["scheduled_count"], 0)
        self.assertEqual(created, 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def _delete_rule(self, rule_id, auth=None):
        async with self.session_factory() as session:
            return await delete_recording_rule(
                rule_id=rule_id, auth=auth or self.auth, session=session
            )

    async def _set_schedule_status(self, epg_suffix, status, *, download_id=None):
        async with self.session_factory() as session:
            program = (
                await session.execute(
                    select(EPGProgram).where(EPGProgram.epg_id == f"epg-{epg_suffix}")
                )
            ).scalar_one()
            schedule = (
                await session.execute(
                    select(ScheduledRecording).where(
                        ScheduledRecording.program_id == str(program.id)
                    )
                )
            ).scalar_one()
            schedule.status = status
            schedule.download_id = download_id
            await session.commit()

    async def _schedules(self):
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(ScheduledRecording).order_by(ScheduledRecording.start_timestamp)
                )
            ).scalars().all()

    async def _scheduled_airings(self):
        """The guide entries that still have a schedule, by the name the test gave them."""
        async with self.session_factory() as session:
            names = {
                str(program.id): program.epg_id
                for program in (await session.execute(select(EPGProgram))).scalars().all()
            }
        return [names.get(schedule.program_id, schedule.program_id) for schedule in await self._schedules()]

    async def test_deleting_rule_removes_recordings_that_have_not_started(self):
        """#427: a deleted rule must not leave orphaned scheduled recordings."""
        await self._add_program(suffix="next-week", hours_from_now=2)
        await self._add_program(suffix="week-after", hours_from_now=26)
        rule = await self._create_rule()
        self.assertEqual(await self._count(ScheduledRecording), 2)

        result = await self._delete_rule(rule["id"])

        self.assertEqual(result, {"status": "deleted", "removed_schedules": 2})
        self.assertEqual(await self._count(RecordingRule), 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_deleting_rule_keeps_recordings_that_started_finished_or_failed(self):
        suffixes = ["downloading", "completed", "failed", "cancelled", "pending"]
        for index, suffix in enumerate(suffixes):
            await self._add_program(suffix=suffix, hours_from_now=2 + index * 3)
        rule = await self._create_rule()
        await self._set_schedule_status("downloading", ScheduledStatus.DOWNLOADING.value, download_id=11)
        await self._set_schedule_status("completed", ScheduledStatus.COMPLETED.value, download_id=12)
        await self._set_schedule_status("failed", ScheduledStatus.FAILED.value, download_id=13)
        await self._set_schedule_status("cancelled", ScheduledStatus.CANCELLED.value)

        result = await self._delete_rule(rule["id"])

        self.assertEqual(result["removed_schedules"], 1)
        self.assertEqual(
            await self._scheduled_airings(),
            ["epg-downloading", "epg-completed", "epg-failed", "epg-cancelled"],
        )
        for schedule in await self._schedules():
            self.assertIsNone(schedule.recording_rule_id, "History is detached, not deleted.")

    async def test_a_recording_paused_for_low_disk_space_has_not_started_either(self):
        await self._add_program(suffix="paused")
        rule = await self._create_rule()
        await self._set_schedule_status("paused", ScheduledStatus.PAUSED_LOW_SPACE.value)

        result = await self._delete_rule(rule["id"])

        self.assertEqual(result["removed_schedules"], 1)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_deleting_rule_leaves_other_recordings_alone(self):
        await self._add_program(suffix="mine")
        await self._add_program(
            suffix="other-rule", channel_id="202", channel_name="Other TV", title="Other Show"
        )
        await self._add_program(suffix="by-hand", title="Picked By Hand", hours_from_now=30)
        rule = await self._create_rule()
        await self._create_rule(channel_id="202", channel_name="Other TV", title_match="Other Show")
        async with self.session_factory() as session:
            program = (
                await session.execute(
                    select(EPGProgram).where(EPGProgram.epg_id == "epg-by-hand")
                )
            ).scalar_one()
            session.add(
                ScheduledRecording(
                    account_id=1,
                    channel_id=program.channel_id,
                    channel_name=program.channel_name,
                    program_id=str(program.id),
                    program_title=program.title,
                    program_start=program.start_time,
                    program_end=program.end_time,
                    start_timestamp=program.start_timestamp,
                    stop_timestamp=program.stop_timestamp,
                    duration_minutes=60,
                    status=ScheduledStatus.SCHEDULED.value,
                    requested_by_user_id=1,
                )
            )
            await session.commit()

        result = await self._delete_rule(rule["id"])

        self.assertEqual(result["removed_schedules"], 1)
        self.assertEqual(
            sorted(await self._scheduled_airings()), ["epg-by-hand", "epg-other-rule"]
        )

    async def test_a_new_rule_can_schedule_what_a_deleted_rule_had_pending(self):
        """Deleting, not cancelling: a kept row would hold the airing's key forever."""
        await self._add_program(suffix="again")
        first = await self._create_rule()
        await self._delete_rule(first["id"])

        second = await self._create_rule()

        self.assertEqual(second["scheduled_count"], 1)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_a_new_rule_does_not_record_again_what_the_deleted_rule_recorded(self):
        await self._add_program(suffix="done")
        first = await self._create_rule()
        await self._set_schedule_status("done", ScheduledStatus.COMPLETED.value, download_id=21)
        await self._delete_rule(first["id"])

        second = await self._create_rule()

        self.assertEqual(second["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_someone_elses_rule_cannot_be_deleted(self):
        await self._add_program(suffix="not-yours")
        rule = await self._create_rule()
        stranger = AuthContext(
            authenticated=True, role="download_only", provider="local", user_id=99, is_admin=False
        )

        with self.assertRaises(HTTPException) as raised:
            await self._delete_rule(rule["id"], auth=stranger)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(await self._count(ScheduledRecording), 1)


    # --- helpers for the cases below -----------------------------------

    async def _set_archive_days(self, days):
        async with self.session_factory() as session:
            account = (
                await session.execute(select(XtreamAccount).where(XtreamAccount.id == 1))
            ).scalar_one()
            account.catchup_max_archive_days = days
            account.catchup_days = days
            await session.commit()

    async def _program(self, suffix):
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(EPGProgram).where(EPGProgram.epg_id == f"epg-{suffix}")
                )
            ).scalar_one()

    async def _schedule_by_hand(self, suffix, status):
        program = await self._program(suffix)
        async with self.session_factory() as session:
            session.add(
                ScheduledRecording(
                    account_id=1,
                    channel_id=program.channel_id,
                    channel_name=program.channel_name,
                    program_id=str(program.id),
                    program_title=program.title,
                    program_start=program.start_time,
                    program_end=program.end_time,
                    start_timestamp=program.start_timestamp,
                    stop_timestamp=program.stop_timestamp,
                    duration_minutes=60,
                    status=status,
                    requested_by_user_id=1,
                )
            )
            await session.commit()

    async def _download_by_hand(self, suffix, status):
        program = await self._program(suffix)
        async with self.session_factory() as session:
            session.add(
                Download(
                    account_id=1,
                    channel_id=program.channel_id,
                    channel_name=program.channel_name,
                    program_title=program.title,
                    program_start=program.start_time,
                    program_end=program.end_time,
                    start_timestamp=program.start_timestamp,
                    stop_timestamp=program.stop_timestamp,
                    duration_minutes=60,
                    source_url="http://provider.test/timeshift",
                    output_path="/recordings/already.ts",
                    status=status,
                    requested_by_user_id=1,
                )
            )
            await session.commit()

    async def _refresh(self):
        async with self.session_factory() as session:
            return await recording_rule_service.evaluate(session, account_id=1)

    # --- an airing that is already taken --------------------------------

    async def test_an_airing_already_recorded_by_hand_is_not_recorded_again(self):
        await self._add_program(suffix="seen")
        await self._schedule_by_hand("seen", ScheduledStatus.COMPLETED.value)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_an_airing_already_downloaded_from_the_guide_is_not_recorded_again(self):
        await self._set_archive_days(7)
        await self._add_program(suffix="grabbed", hours_from_now=-3)
        await self._download_by_hand("grabbed", DownloadStatus.COMPLETED.value)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_an_airing_scheduled_by_hand_and_still_waiting_is_left_to_it(self):
        await self._add_program(suffix="waiting")
        await self._schedule_by_hand("waiting", ScheduledStatus.SCHEDULED.value)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_a_cancelled_or_failed_attempt_by_hand_does_not_block_the_rule(self):
        await self._add_program(suffix="cancelled", hours_from_now=2)
        await self._add_program(suffix="failed", hours_from_now=5)
        await self._schedule_by_hand("cancelled", ScheduledStatus.CANCELLED.value)
        await self._schedule_by_hand("failed", ScheduledStatus.FAILED.value)
        await self._download_by_hand("failed", DownloadStatus.FAILED.value)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 2)

    async def test_removing_a_finished_recording_from_history_does_not_record_it_again(self):
        """A guide refresh only looks forward, so it cannot resurrect an ended airing."""
        await self._set_archive_days(7)
        await self._add_program(suffix="aired", hours_from_now=-3)
        rule = await self._create_rule()
        self.assertEqual(rule["scheduled_count"], 1)
        async with self.session_factory() as session:
            schedule = (await session.execute(select(ScheduledRecording))).scalar_one()
            await session.delete(schedule)
            await session.commit()

        created = await self._refresh()

        self.assertEqual(created, 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    # --- catching up when a rule is saved --------------------------------

    async def test_saving_a_rule_catches_what_finished_recently(self):
        await self._set_archive_days(7)
        await self._add_program(suffix="last-night", hours_from_now=-(BACKFILL_HOURS - 2))

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 1)

    async def test_saving_a_rule_does_not_queue_the_whole_catchup_archive(self):
        """An hourly title on a week of catchup used to queue over 160 at once."""
        await self._set_archive_days(7)
        for hours_ago in range(BACKFILL_HOURS + 2, 24 * 6, 6):
            await self._add_program(suffix=f"old-{hours_ago}", hours_from_now=-hours_ago)
        await self._add_program(suffix="recent", hours_from_now=-3)
        await self._add_program(suffix="upcoming", hours_from_now=3)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 2)

    async def test_catchup_shorter_than_a_day_limits_it_further(self):
        await self._set_archive_days(0)
        await self._add_program(suffix="no-catchup", hours_from_now=-3)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)

    async def test_a_guide_refresh_does_not_reach_back_at_all(self):
        await self._set_archive_days(7)
        rule = await self._create_rule(enabled=False)
        await self._add_program(suffix="ended", hours_from_now=-3)
        await self._add_program(suffix="on-air", hours_from_now=-0.5)
        async with self.session_factory() as session:
            stored = (await session.execute(select(RecordingRule))).scalar_one()
            stored.enabled = True
            await session.commit()

        created = await self._refresh()

        self.assertEqual(created, 1, "Only the programme still on air.")
        self.assertEqual(rule["scheduled_count"], 0)

    # --- loose patterns ---------------------------------------------------

    async def _add_many(self, count):
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        async with self.session_factory() as session:
            for index in range(count):
                begin = start + timedelta(hours=index)
                end = begin + timedelta(hours=1)
                session.add(
                    EPGProgram(
                        account_id=1,
                        channel_id="101",
                        channel_name="Example TV",
                        epg_id=f"epg-many-{index}",
                        title=f"Programme about a thing {index}",
                        start_time=begin,
                        end_time=end,
                        start_timestamp=int(begin.timestamp()),
                        stop_timestamp=int(end.timestamp()),
                        duration_minutes=60,
                        has_archive=True,
                    )
                )
            await session.commit()

    async def test_a_rule_keeps_a_bounded_number_of_recordings_waiting(self):
        await self._add_many(MAX_PENDING_SCHEDULES_PER_RULE + 40)

        rule = await self._create_rule(title_match="a", match_mode="contains")

        self.assertEqual(rule["scheduled_count"], MAX_PENDING_SCHEDULES_PER_RULE)
        schedules = await self._schedules()
        self.assertEqual(len(schedules), MAX_PENDING_SCHEDULES_PER_RULE)
        soonest = min(schedule.start_timestamp for schedule in schedules)
        self.assertLess(soonest - datetime.now(timezone.utc).timestamp(), 2 * 3600, "Nearest first.")

    async def test_the_rest_are_scheduled_as_the_first_ones_are_recorded(self):
        await self._add_many(MAX_PENDING_SCHEDULES_PER_RULE + 40)
        await self._create_rule(title_match="a", match_mode="contains")
        async with self.session_factory() as session:
            first_ten = (
                await session.execute(
                    select(ScheduledRecording)
                    .order_by(ScheduledRecording.start_timestamp)
                    .limit(10)
                )
            ).scalars().all()
            for schedule in first_ten:
                schedule.status = ScheduledStatus.COMPLETED.value
            await session.commit()

        created = await self._refresh()

        self.assertEqual(created, 10)

    async def test_a_very_long_title_pattern_is_refused(self):
        with self.assertRaises(ValidationError):
            RecordingRuleCreate(
                account_id=1,
                channel_id="101",
                channel_name="Example TV",
                title_match="x" * (MAX_TITLE_MATCH_LENGTH + 1),
            )
        with self.assertRaises(ValidationError):
            RecordingRuleUpdate(title_match="x" * (MAX_TITLE_MATCH_LENGTH + 1))

    # --- regex is for admins ----------------------------------------------
    # A pattern runs against every guide title on every refresh, on the
    # event loop, so one bad pattern can stall the server for everyone.

    async def _add_viewer(self):
        async with self.session_factory() as session:
            session.add(User(id=2, role="download_only", username="alice"))
            await session.commit()
        return AuthContext(
            authenticated=True, role="download_only", provider="local", user_id=2, is_admin=False
        )

    async def _create_as(self, auth, **fields):
        defaults = dict(
            account_id=1, channel_id="101", channel_name="Example TV", title_match="The Example Show"
        )
        defaults.update(fields)
        async with self.session_factory() as session:
            return await create_recording_rule(
                data=RecordingRuleCreate(**defaults), auth=auth, session=session
            )

    async def test_a_download_only_user_cannot_create_a_regex_rule(self):
        viewer = await self._add_viewer()

        with self.assertRaises(HTTPException) as raised:
            await self._create_as(viewer, title_match="^(.*?,?){12}Z$", match_mode="regex")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(await self._count(RecordingRule), 0)

    async def test_a_download_only_user_cannot_turn_their_rule_into_a_regex(self):
        viewer = await self._add_viewer()
        rule = await self._create_as(viewer)

        with self.assertRaises(HTTPException) as raised:
            async with self.session_factory() as session:
                await update_recording_rule(
                    rule_id=rule["id"],
                    data=RecordingRuleUpdate(match_mode="regex", title_match="^News"),
                    auth=viewer,
                    session=session,
                )

        self.assertEqual(raised.exception.status_code, 403)

    async def test_exact_and_contains_stay_open_to_everyone(self):
        viewer = await self._add_viewer()
        await self._add_program()

        exact = await self._create_as(viewer)
        contains = await self._create_as(
            viewer, channel_id="202", title_match="example", match_mode="contains"
        )

        self.assertEqual(exact["scheduled_count"], 1)
        self.assertEqual(contains["match_mode"], "contains")

    async def test_an_admin_can_still_use_a_regex(self):
        await self._add_program()

        rule = await self._create_as(self.auth, title_match="^the example", match_mode="regex")

        self.assertEqual(rule["scheduled_count"], 1)

    # --- rules do not outlive their owner ---------------------------------

    async def test_deleting_a_user_deletes_their_rules(self):
        alice = await self._add_viewer()
        await self._add_program(suffix="alices")
        async with self.session_factory() as session:
            await create_recording_rule(
                data=RecordingRuleCreate(
                    account_id=1, channel_id="101", channel_name="Example TV",
                    title_match="The Example Show",
                ),
                auth=alice,
                session=session,
            )
        self.assertEqual(await self._count(ScheduledRecording), 1)

        with (
            patch("api.admin_users.download_manager.cancel_download", AsyncMock()),
            patch("api.admin_users.download_manager.disconnect_user_websockets", AsyncMock()),
        ):
            async with self.session_factory() as session:
                await delete_user(user_id=2, _admin=self.auth, session=session)

        self.assertEqual(await self._count(RecordingRule), 0)
        await self._add_program(suffix="after-she-left", hours_from_now=30)
        self.assertEqual(await self._refresh(), 0, "Nothing is left to schedule for her.")

    async def test_deleting_an_account_deletes_its_rules(self):
        await self._add_program(suffix="on-the-account")
        await self._create_rule()

        with (
            patch("api.accounts.download_manager.cancel_download", AsyncMock()),
            patch("api.accounts.epg_service.clear_cache", MagicMock()),
        ):
            async with self.session_factory() as session:
                await delete_account(account_id=1, _admin=None, session=session)

        self.assertEqual(await self._count(RecordingRule), 0)
        for schedule in await self._schedules():
            self.assertIsNone(schedule.recording_rule_id)

    # --- a listing the provider moved -------------------------------------
    #
    # A guide refresh adds the retimed listing next to the old one (rows are
    # keyed on channel + start + stop), so after a move the guide holds both.

    async def _add_listing(
        self, suffix, *, starts_in_minutes, minutes=60, title="The Example Show"
    ):
        if not hasattr(self, "_guide_now"):
            self._guide_now = datetime.now(timezone.utc).replace(microsecond=0)
        start = self._guide_now + timedelta(minutes=starts_in_minutes)
        end = start + timedelta(minutes=minutes)
        async with self.session_factory() as session:
            session.add(
                EPGProgram(
                    account_id=1,
                    channel_id="101",
                    channel_name="Example TV",
                    epg_id=f"epg-{suffix}",
                    title=title,
                    start_time=start,
                    end_time=end,
                    start_timestamp=int(start.timestamp()),
                    stop_timestamp=int(end.timestamp()),
                    duration_minutes=minutes,
                    has_archive=True,
                )
            )
            await session.commit()

    async def _assert_recording_is_for(self, schedule, suffix):
        program = await self._program(suffix)
        self.assertEqual(
            (schedule.program_id, schedule.start_timestamp, schedule.stop_timestamp),
            (str(program.id), program.start_timestamp, program.stop_timestamp),
        )

    async def test_a_listing_moved_by_a_few_minutes_is_not_recorded_twice(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule()
        await self._add_listing("moved", starts_in_minutes=125)

        created = await self._refresh()

        self.assertEqual(created, 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)

    async def test_the_waiting_recording_follows_the_listing_to_its_new_time(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule()
        await self._add_listing("moved", starts_in_minutes=125, minutes=65)

        await self._refresh()

        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "moved")
        self.assertEqual(schedule.duration_minutes, 65)
        self.assertEqual(schedule.status, ScheduledStatus.SCHEDULED.value)
        moved = await self._program("moved")
        self.assertEqual(
            int(schedule.program_start.replace(tzinfo=timezone.utc).timestamp()),
            moved.start_timestamp,
        )
        self.assertEqual(
            int(schedule.program_end.replace(tzinfo=timezone.utc).timestamp()),
            moved.stop_timestamp,
        )

    async def test_the_old_listing_still_in_the_guide_does_not_pull_it_back(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule()
        await self._add_listing("moved", starts_in_minutes=125)
        await self._refresh()

        created = await self._refresh()

        self.assertEqual(created, 0)
        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "moved")

    async def test_a_rule_saved_while_both_listings_are_in_the_guide_records_once(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._add_listing("moved", starts_in_minutes=125)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 1)
        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "moved")

    async def test_a_recording_that_has_already_started_is_left_where_it_is(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule()
        await self._set_schedule_status(
            "as-first-listed", ScheduledStatus.DOWNLOADING.value, download_id=7
        )
        await self._add_listing("moved", starts_in_minutes=125)

        created = await self._refresh()

        self.assertEqual(created, 0)
        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "as-first-listed")

    async def test_a_listing_that_moved_as_it_was_ending_is_not_recorded_twice(self):
        """The first listing has just ended, the moved one is still on air."""
        await self._set_archive_days(7)
        await self._add_listing("as-first-listed", starts_in_minutes=-62)
        await self._create_rule()
        self.assertEqual(await self._count(ScheduledRecording), 1)
        await self._add_listing("moved", starts_in_minutes=-57)

        created = await self._refresh()

        self.assertEqual(created, 0)
        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "moved")

    async def test_back_to_back_episodes_that_both_move_stay_two_recordings(self):
        await self._add_listing("first", starts_in_minutes=120, minutes=30)
        await self._add_listing("second", starts_in_minutes=150, minutes=30)
        await self._create_rule()
        await self._add_listing("first-moved", starts_in_minutes=125, minutes=30)
        await self._add_listing("second-moved", starts_in_minutes=155, minutes=30)

        created = await self._refresh()

        self.assertEqual(created, 0)
        first, second = await self._schedules()
        await self._assert_recording_is_for(first, "first-moved")
        await self._assert_recording_is_for(second, "second-moved")

    async def test_a_repeat_later_the_same_day_is_still_its_own_recording(self):
        await self._add_listing("evening", starts_in_minutes=120)
        await self._add_listing("late-repeat", starts_in_minutes=300)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 2)

    async def test_a_different_programme_put_in_the_slot_is_still_recorded(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule(title_match="Example", match_mode="contains")
        await self._add_listing(
            "replacement", starts_in_minutes=125, title="Example Special"
        )

        created = await self._refresh()

        self.assertEqual(created, 1)
        self.assertEqual(await self._count(ScheduledRecording), 2)

    async def test_a_recording_scheduled_by_hand_covers_the_moved_listing_too(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._schedule_by_hand("as-first-listed", ScheduledStatus.SCHEDULED.value)
        await self._add_listing("moved", starts_in_minutes=125)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)
        (schedule,) = await self._schedules()
        await self._assert_recording_is_for(schedule, "as-first-listed")

    async def test_an_episode_already_downloaded_is_not_recorded_again_after_a_move(self):
        await self._set_archive_days(7)
        await self._add_listing("as-first-listed", starts_in_minutes=-180)
        await self._download_by_hand("as-first-listed", DownloadStatus.COMPLETED.value)
        await self._add_listing("moved", starts_in_minutes=-175)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 0)
        self.assertEqual(await self._count(ScheduledRecording), 0)

    async def test_a_failed_download_does_not_stop_the_moved_listing_being_recorded(self):
        await self._set_archive_days(7)
        await self._add_listing("as-first-listed", starts_in_minutes=-180)
        await self._download_by_hand("as-first-listed", DownloadStatus.FAILED.value)
        await self._add_listing("moved", starts_in_minutes=-175)

        rule = await self._create_rule()

        self.assertEqual(rule["scheduled_count"], 1)

    async def test_a_recording_you_cancelled_stays_cancelled_when_the_listing_moves(self):
        await self._add_listing("as-first-listed", starts_in_minutes=120)
        await self._create_rule()
        await self._set_schedule_status("as-first-listed", ScheduledStatus.CANCELLED.value)
        await self._add_listing("moved", starts_in_minutes=125)

        created = await self._refresh()

        self.assertEqual(created, 0)
        (schedule,) = await self._schedules()
        self.assertEqual(schedule.status, ScheduledStatus.CANCELLED.value)
        await self._assert_recording_is_for(schedule, "as-first-listed")


if __name__ == "__main__":
    unittest.main()
