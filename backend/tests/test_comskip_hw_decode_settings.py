"""Settings surface for the Comskip hardware-decode mode (issue #429).

Fresh installs and upgraded installs both report "none", the value round-trips
through the settings endpoint, and anything unrecognised is coerced to "none"
rather than rejected.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import Base, _apply_lightweight_migrations
from models import AppSettings
from api.settings import SettingsUpdate, update_settings


class ComskipHwDecodeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed(self, **values):
        async with self.session_factory() as session:
            session.add(AppSettings(**values))
            await session.commit()

    async def _update(self, **update):
        async with self.session_factory() as session:
            return await update_settings(
                update_data=SettingsUpdate(**update),
                _admin=None,
                session=session,
            )

    async def test_fresh_install_defaults_to_none(self):
        await self._seed()
        result = await self._update(comskip_enabled=True)
        self.assertEqual(result["comskip_hw_decode_mode"], "none")

    async def test_valid_modes_round_trip(self):
        await self._seed()
        for mode in ("hwassist", "nvidia", "none"):
            result = await self._update(comskip_hw_decode_mode=mode)
            self.assertEqual(result["comskip_hw_decode_mode"], mode)

    async def test_unknown_mode_is_coerced_to_none(self):
        await self._seed()
        result = await self._update(comskip_hw_decode_mode="quicksync")
        self.assertEqual(result["comskip_hw_decode_mode"], "none")

    async def test_mode_is_case_and_space_insensitive(self):
        await self._seed()
        result = await self._update(comskip_hw_decode_mode="  NVIDIA ")
        self.assertEqual(result["comskip_hw_decode_mode"], "nvidia")


class ComskipHwDecodeMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgraded_install_gets_the_column_defaulting_to_none(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with session_factory() as session:
                session.add(AppSettings())
                await session.commit()

            # Simulate a database that predates the column.
            async with engine.begin() as conn:
                await conn.execute(text(
                    "ALTER TABLE app_settings DROP COLUMN comskip_hw_decode_mode"
                ))
                await _apply_lightweight_migrations(conn)
                value = (await conn.execute(text(
                    "SELECT comskip_hw_decode_mode FROM app_settings"
                ))).scalar_one()
            self.assertEqual(value, "none")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
