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
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    EPGProgram,
    RecordingRule,
    ScheduledRecording,
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
        delete_after_days=None,
        match_mode="exact",
    ):
        async with self.session_factory() as session:
            return await create_recording_rule(
                data=RecordingRuleCreate(
                    account_id=1,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    title_match=title_match,
                    enabled=enabled,
                    delete_after_days=delete_after_days,
                    match_mode=match_mode,
                ),
                auth=self.auth,
                session=session,
            )

    async def _count(self, model):
        async with self.session_factory() as session:
            return (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()

    async def _link_completed_download(self, rule_id, output_path, *, age_days=8):
        completed_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        async with self.session_factory() as session:
            schedule = (
                await session.execute(
                    select(ScheduledRecording).where(
                        ScheduledRecording.recording_rule_id == rule_id
                    )
                )
            ).scalar_one()
            download = Download(
                account_id=1,
                channel_id=schedule.channel_id,
                channel_name=schedule.channel_name,
                program_title=schedule.program_title,
                program_start=schedule.program_start,
                program_end=schedule.program_end,
                start_timestamp=schedule.start_timestamp,
                stop_timestamp=schedule.stop_timestamp,
                duration_minutes=schedule.duration_minutes,
                source_url="http://provider.test/recording.ts",
                output_path=str(output_path),
                requested_by_user_id=1,
                request_source="admin_local",
                status=DownloadStatus.COMPLETED.value,
                completed_at=completed_at,
            )
            session.add(download)
            await session.flush()
            schedule.download_id = download.id
            schedule.status = "completed"
            await session.commit()
            return download.id

    async def test_creating_rule_matches_future_epg_item(self):
        await self._add_program()

        result = await self._create_rule()

        self.assertEqual(result["title_match"], "The Example Show")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(await self._count(RecordingRule), 1)
        self.assertEqual(await self._count(ScheduledRecording), 1)

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
                    delete_after_days=14,
                    pre_padding_minutes=2,
                    post_padding_minutes=8,
                ),
                auth=self.auth,
                session=session,
            )

        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["match_mode"], "contains")
        self.assertEqual(result["delete_after_days"], 14)
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

    async def test_deleting_rule_keeps_already_created_schedule(self):
        await self._add_program(suffix="delete")
        rule = await self._create_rule()

        async with self.session_factory() as session:
            result = await delete_recording_rule(
                rule_id=rule["id"], auth=self.auth, session=session
            )

        self.assertEqual(result, {"status": "deleted"})
        self.assertEqual(await self._count(RecordingRule), 0)
        self.assertEqual(await self._count(ScheduledRecording), 1)
        async with self.session_factory() as session:
            schedule = (
                await session.execute(select(ScheduledRecording))
            ).scalar_one()
            self.assertIsNone(schedule.recording_rule_id)
            self.assertIsNotNone(schedule.rule_airing_key)

    async def test_optional_retention_deletes_only_expired_rule_download(self):
        await self._add_program(suffix="retention")
        rule = await self._create_rule(delete_after_days=7)
        recording = Path(self.recording_dir.name) / "expired.ts"
        recording.write_bytes(b"recording")
        await self._link_completed_download(rule["id"], recording, age_days=8)

        async with self.session_factory() as session:
            deleted = await recording_rule_service.delete_expired_downloads(
                session,
                rule_id=rule["id"],
                now=datetime.now(timezone.utc),
            )

        self.assertEqual(deleted, 1)
        self.assertFalse(recording.exists())
        self.assertEqual(await self._count(Download), 0)
        async with self.session_factory() as session:
            schedule = (
                await session.execute(select(ScheduledRecording))
            ).scalar_one()
            self.assertIsNone(schedule.download_id)
            self.assertIn("deleted after 7 days", schedule.status_message)

    async def test_keep_forever_does_not_delete_completed_download(self):
        await self._add_program(suffix="keep-forever")
        rule = await self._create_rule()
        recording = Path(self.recording_dir.name) / "kept.ts"
        recording.write_bytes(b"recording")
        await self._link_completed_download(rule["id"], recording, age_days=30)

        async with self.session_factory() as session:
            deleted = await recording_rule_service.delete_expired_downloads(
                session, rule_id=rule["id"]
            )

        self.assertEqual(deleted, 0)
        self.assertTrue(recording.exists())
        self.assertEqual(await self._count(Download), 1)

    async def test_retention_refuses_file_outside_configured_roots(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        await self._add_program(suffix="unsafe-retention")
        rule = await self._create_rule(delete_after_days=7)
        recording = Path(outside_dir.name) / "outside.ts"
        recording.write_bytes(b"recording")
        await self._link_completed_download(rule["id"], recording, age_days=8)

        async with self.session_factory() as session:
            deleted = await recording_rule_service.delete_expired_downloads(
                session, rule_id=rule["id"]
            )

        self.assertEqual(deleted, 0)
        self.assertTrue(recording.exists())
        self.assertEqual(await self._count(Download), 1)


if __name__ == "__main__":
    unittest.main()
