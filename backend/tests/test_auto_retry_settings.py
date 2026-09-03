"""Validation and persistence tests for the automatic retry schedule."""
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.settings import SettingsUpdate, update_settings
from database import Base
from models import AppSettings


class AutoRetrySettingsValidationTests(unittest.TestCase):
    def test_accepts_valid_retry_schedule(self):
        update = SettingsUpdate(auto_retry_backoff_minutes=[5, 10, 15, 60])
        self.assertEqual(update.auto_retry_backoff_minutes, [5, 10, 15, 60])

    def test_rejects_empty_or_out_of_range_retry_schedule(self):
        for value in ([], [0], [1441], list(range(1, 12))):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SettingsUpdate(auto_retry_backoff_minutes=value)

    def test_invalid_stored_value_falls_back_to_defaults(self):
        settings = AppSettings(auto_retry_backoff_minutes="not,a,schedule")
        self.assertEqual(settings.get_auto_retry_backoff_minutes(), [5, 10, 15, 60])


class AutoRetrySettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_update_serializes_and_returns_retry_schedule(self):
        async with self.session_factory() as session:
            session.add(AppSettings())
            await session.commit()
            response = await update_settings(
                update_data=SettingsUpdate(auto_retry_backoff_minutes=[2, 8, 30]),
                _admin=None,
                session=session,
            )
            result = await session.execute(select(AppSettings))
            stored = result.scalar_one()

        self.assertEqual(stored.auto_retry_backoff_minutes, "2,8,30")
        self.assertEqual(response["auto_retry_backoff_minutes"], [2, 8, 30])


if __name__ == "__main__":
    unittest.main()
