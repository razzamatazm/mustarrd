"""
Tests for schedule export/import (JSON backup & restore).

- GET /schedules/export returns a versioned document with the fields needed to
  recreate schedules (no internal state); non-admins only see their own rows.
- POST /schedules/import recreates schedules through the normal creation path:
  duplicates, already-ended programs and unknown accounts are skipped and
  reported in the summary instead of failing the whole import.
- Documents with an unsupported version are rejected with 400.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import ScheduledRecording, User, XtreamAccount
from api.schedules import (
    SCHEDULE_EXPORT_FORMAT,
    SCHEDULE_EXPORT_VERSION,
    ScheduleCreate,
    ScheduleExportItem,
    ScheduleImportDocument,
    create_schedule,
    export_schedules,
    import_schedules,
)


def _future_program(epg_id="ep-1", title="Newsnight", hours_from_now=1):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    stop = start + timedelta(hours=1)
    return {
        "title": title,
        "description": "A late-night news programme",
        "epg_id": epg_id,
        "id": "prog-1",
        "category": "News",
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(stop.timestamp()),
    }


def _auth(user_id, is_admin=False):
    auth = MagicMock()
    auth.user_id = user_id
    auth.is_admin = is_admin
    auth.provider = "admin_local" if is_admin else "local"
    return auth


class ScheduleExportImportTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                id=1, name="main", server_url="http://provider.test",
                username="u", password="p",
            ))
            session.add(User(id=1, role="admin", username="admin"))
            session.add(User(id=2, role="download_only", username="alice"))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _create(self, auth, program, **kwargs):
        data = ScheduleCreate(
            account_id=1,
            channel_id=kwargs.pop("channel_id", "CH1"),
            channel_name=kwargs.pop("channel_name", "BBC One"),
            program=program,
            **kwargs,
        )
        async with self.session_factory() as session:
            return await create_schedule(data=data, auth=auth, session=session)

    async def _export(self, auth):
        async with self.session_factory() as session:
            return await export_schedules(auth=auth, session=session)

    async def _import(self, auth, doc):
        async with self.session_factory() as session:
            return await import_schedules(doc=doc, auth=auth, session=session)

    async def _all_schedules(self):
        async with self.session_factory() as session:
            result = await session.execute(
                select(ScheduledRecording).order_by(ScheduledRecording.id)
            )
            return result.scalars().all()

    async def _wipe_schedules(self):
        async with self.session_factory() as session:
            await session.execute(sql_delete(ScheduledRecording))
            await session.commit()

    async def test_round_trip_recreates_schedules(self):
        """Export then import must recreate the schedules with the same
        channel, program identity/times, account, padding and filename."""
        prog_a = _future_program(epg_id="ep-1", title="Show A", hours_from_now=1)
        prog_b = _future_program(epg_id="ep-2", title="Show B", hours_from_now=3)
        await self._create(
            _auth(1, is_admin=True), prog_a,
            pre_padding_minutes=2, post_padding_minutes=7,
            custom_filename="My Show",
        )
        await self._create(_auth(1, is_admin=True), prog_b, channel_id="CH2", channel_name="BBC Two")

        doc = await self._export(_auth(1, is_admin=True))
        self.assertEqual(doc["format"], SCHEDULE_EXPORT_FORMAT)
        self.assertEqual(doc["version"], SCHEDULE_EXPORT_VERSION)
        self.assertEqual(len(doc["schedules"]), 2)
        for item in doc["schedules"]:
            self.assertNotIn("download_id", item, "Internal state must not be exported.")
            self.assertNotIn("status", item, "Internal state must not be exported.")
            self.assertEqual(item["account_name"], "main")

        await self._wipe_schedules()

        result = await self._import(
            _auth(1, is_admin=True), ScheduleImportDocument.model_validate(doc)
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["skipped"], [])

        schedules = await self._all_schedules()
        self.assertEqual(len(schedules), 2)
        recreated = {s.program_title: s for s in schedules}
        show_a = recreated["Show A"]
        self.assertEqual(show_a.channel_id, "CH1")
        self.assertEqual(show_a.start_timestamp, prog_a["start_timestamp"])
        self.assertEqual(show_a.stop_timestamp, prog_a["stop_timestamp"])
        self.assertEqual(show_a.epg_id, "ep-1")
        self.assertEqual(show_a.pre_padding_minutes, 2)
        self.assertEqual(show_a.post_padding_minutes, 7)
        self.assertEqual(show_a.custom_filename, "My Show.ts")
        self.assertEqual(show_a.account_id, 1)
        show_b = recreated["Show B"]
        self.assertEqual(show_b.channel_id, "CH2")
        self.assertEqual(show_b.channel_name, "BBC Two")

    async def test_import_skips_duplicates(self):
        """Re-importing an export over existing schedules must skip every
        entry as a duplicate and report it, creating nothing."""
        await self._create(_auth(1, is_admin=True), _future_program(title="Dup Show"))
        doc = await self._export(_auth(1, is_admin=True))

        result = await self._import(
            _auth(1, is_admin=True), ScheduleImportDocument.model_validate(doc)
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["reason"], "Already scheduled")
        self.assertEqual(result["skipped"][0]["title"], "Dup Show")
        self.assertEqual(len(await self._all_schedules()), 1)

    async def test_import_rejects_bad_version(self):
        doc = ScheduleImportDocument(
            format=SCHEDULE_EXPORT_FORMAT, version=99, schedules=[]
        )
        with self.assertRaises(HTTPException) as ctx:
            await self._import(_auth(1, is_admin=True), doc)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("version", str(ctx.exception.detail))

    async def test_non_admin_export_only_sees_own_schedules(self):
        await self._create(_auth(1, is_admin=True), _future_program(epg_id="ep-1", title="Admin Show"))
        await self._create(
            _auth(2), _future_program(epg_id="ep-2", title="Alice Show", hours_from_now=4),
            channel_id="CH9",
        )

        alice_doc = await self._export(_auth(2))
        self.assertEqual(len(alice_doc["schedules"]), 1)
        self.assertEqual(alice_doc["schedules"][0]["program_title"], "Alice Show")

        admin_doc = await self._export(_auth(1, is_admin=True))
        self.assertEqual(len(admin_doc["schedules"]), 2)

    async def test_import_skips_expired_program(self):
        now = datetime.now(timezone.utc)
        doc = ScheduleImportDocument(
            version=SCHEDULE_EXPORT_VERSION,
            schedules=[ScheduleExportItem(
                account_id=1,
                channel_id="CH1",
                channel_name="BBC One",
                program_title="Old Show",
                start_timestamp=int((now - timedelta(hours=2)).timestamp()),
                stop_timestamp=int((now - timedelta(hours=1)).timestamp()),
            )],
        )

        result = await self._import(_auth(1, is_admin=True), doc)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "Program already ended")
        self.assertEqual(result["skipped"][0]["title"], "Old Show")
        self.assertEqual(len(await self._all_schedules()), 0)

    async def test_import_skips_unknown_account(self):
        program = _future_program(title="Orphan Show")
        doc = ScheduleImportDocument(
            version=SCHEDULE_EXPORT_VERSION,
            schedules=[ScheduleExportItem(
                account_id=999,
                account_name="no-such-account",
                channel_id="CH1",
                channel_name="BBC One",
                program_title="Orphan Show",
                start_timestamp=program["start_timestamp"],
                stop_timestamp=program["stop_timestamp"],
            )],
        )

        result = await self._import(_auth(1, is_admin=True), doc)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "Account not found")
        self.assertEqual(len(await self._all_schedules()), 0)

    async def test_import_resolves_account_by_name(self):
        """An export from another install can reference the account by name
        when the numeric id does not match."""
        program = _future_program(title="Renumbered Show")
        doc = ScheduleImportDocument(
            version=SCHEDULE_EXPORT_VERSION,
            schedules=[ScheduleExportItem(
                account_id=42,  # wrong id, but the name matches
                account_name="main",
                channel_id="CH1",
                channel_name="BBC One",
                program_title="Renumbered Show",
                start_timestamp=program["start_timestamp"],
                stop_timestamp=program["stop_timestamp"],
            )],
        )

        result = await self._import(_auth(1, is_admin=True), doc)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], [])
        schedules = await self._all_schedules()
        self.assertEqual(schedules[0].account_id, 1)


if __name__ == "__main__":
    unittest.main()
