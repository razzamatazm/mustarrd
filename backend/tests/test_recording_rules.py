import sys
import tempfile
import unittest
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
from models import (
    AppSettings,
    EPGProgram,
    RecordingRule,
    ScheduledRecording,
    ScheduledStatus,
    User,
    XtreamAccount,
)
from services.recording_rule_service import recording_rule_service


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


if __name__ == "__main__":
    unittest.main()
