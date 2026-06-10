"""Migration test: legacy comskip_ini_path carry-over.

Before the Comskip settings editor, GET /settings auto-filled
comskip_ini_path with the config-dir default for every install, so a
non-null value does NOT mean the user chose a custom INI.  Only a value
different from the default is a real user choice and must be copied into
comskip_custom_ini_path so it keeps overriding the generated INI.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import Base, _carry_over_legacy_comskip_ini
from models import AppSettings


class LegacyComskipIniCarryOverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _run_carry_over(self):
        with patch("config.ensure_config_files", return_value=Path("/cfg")):
            async with self.engine.begin() as conn:
                await _carry_over_legacy_comskip_ini(conn)

    async def _fetch(self):
        async with self.session_factory() as session:
            result = await session.execute(select(AppSettings))
            return result.scalar_one()

    async def test_custom_path_copied(self):
        async with self.session_factory() as session:
            session.add(AppSettings(comskip_ini_path="/home/user/my-tuned.ini"))
            await session.commit()

        await self._run_carry_over()

        settings = await self._fetch()
        self.assertEqual(settings.comskip_custom_ini_path, "/home/user/my-tuned.ini")

    async def test_autofilled_default_not_copied(self):
        async with self.session_factory() as session:
            session.add(AppSettings(comskip_ini_path="/cfg/comskip.ini"))
            await session.commit()

        await self._run_carry_over()

        settings = await self._fetch()
        self.assertIsNone(settings.comskip_custom_ini_path)

    async def test_alternate_spelling_of_default_not_copied(self):
        """Path comparison must be normalization-tolerant, not raw string equality."""
        async with self.session_factory() as session:
            session.add(AppSettings(comskip_ini_path="/cfg/../cfg/comskip.ini"))
            await session.commit()

        await self._run_carry_over()

        settings = await self._fetch()
        self.assertIsNone(settings.comskip_custom_ini_path)

    async def test_null_path_not_copied(self):
        async with self.session_factory() as session:
            session.add(AppSettings(comskip_ini_path=None))
            await session.commit()

        await self._run_carry_over()

        settings = await self._fetch()
        self.assertIsNone(settings.comskip_custom_ini_path)


if __name__ == "__main__":
    unittest.main()
