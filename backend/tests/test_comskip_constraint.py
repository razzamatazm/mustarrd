"""
Regression tests for ComSkip settings constraints.

Two related bugs:

Bug 1 (HIGH): update_settings only enforced "comskip_enabled=True forces
transcode_enabled=True" when the current request contained comskip_enabled.
A second request setting transcode_enabled=False bypassed the constraint,
leaving comskip_enabled=True + transcode_enabled=False in the DB. ComSkip
ran and produced an EDL that was silently ignored; commercials stayed in
the output with no user-visible error.

Bug 2 (MEDIUM): No constraint enforced "comskip_enabled=True forces
remux_only=False". A user could store comskip_enabled=True + remux_only=True.
FFmpeg ran in stream-copy mode and never applied EDL cut points. All
commercial segments remained in the output file while the UI showed COMPLETED.

Fix: constraints now apply to the final stored state after all field updates,
not just to the fields present in the current request body.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from database import Base
from models import AppSettings
from api.settings import SettingsUpdate, update_settings


class ComSkipConstraintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _apply(self, initial: dict, update: dict) -> dict:
        """Create settings with `initial` values, apply `update`, return result dict."""
        async with self.session_factory() as session:
            settings = AppSettings(**initial)
            session.add(settings)
            await session.commit()

        async with self.session_factory() as session:
            return await update_settings(
                update_data=SettingsUpdate(**update),
                _admin=None,
                session=session,
            )

    async def test_second_request_cannot_disable_transcode_while_comskip_on(self):
        """PUT transcode_enabled=False must be ignored when comskip_enabled is already True.

        Before fix: settings stored comskip_enabled=True, transcode_enabled=False.
        After fix: transcode_enabled stays True regardless of what the request sets.
        """
        result = await self._apply(
            initial={"comskip_enabled": True, "transcode_enabled": True},
            update={"transcode_enabled": False},
        )
        self.assertTrue(result["transcode_enabled"],
            "transcode_enabled must stay True when comskip_enabled is already True")
        self.assertTrue(result["comskip_enabled"])

    async def test_second_request_cannot_enable_remux_while_comskip_on(self):
        """PUT remux_only=True must be rejected when comskip_enabled is already True.

        Before fix: settings stored comskip_enabled=True, remux_only=True. ComSkip
        ran but FFmpeg stream-copied; commercials remained in output.
        After fix: remux_only stays False regardless of what the request sets.
        """
        result = await self._apply(
            initial={"comskip_enabled": True, "remux_only": False},
            update={"remux_only": True},
        )
        self.assertFalse(result["remux_only"],
            "remux_only must stay False when comskip_enabled is already True")
        self.assertTrue(result["comskip_enabled"])

    async def test_enabling_comskip_forces_transcode_on(self):
        """Single request enabling comskip must auto-enable transcode (pre-existing behaviour)."""
        result = await self._apply(
            initial={"comskip_enabled": False, "transcode_enabled": False},
            update={"comskip_enabled": True},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertTrue(result["transcode_enabled"],
            "enabling comskip must force transcode_enabled=True")

    async def test_enabling_comskip_forces_remux_off(self):
        """Single request enabling comskip must auto-disable remux_only."""
        result = await self._apply(
            initial={"comskip_enabled": False, "remux_only": True},
            update={"comskip_enabled": True},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertFalse(result["remux_only"],
            "enabling comskip must force remux_only=False")

    async def test_enabling_comskip_with_conflicting_values_in_same_request(self):
        """comskip_enabled=True + transcode_enabled=False in the same request: comskip wins."""
        result = await self._apply(
            initial={"comskip_enabled": False, "transcode_enabled": True},
            update={"comskip_enabled": True, "transcode_enabled": False, "remux_only": True},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertTrue(result["transcode_enabled"],
            "comskip constraint must win over explicit transcode_enabled=False in same request")
        self.assertFalse(result["remux_only"],
            "comskip constraint must win over explicit remux_only=True in same request")

    async def test_transcode_can_be_disabled_when_comskip_is_off(self):
        """transcode_enabled=False is accepted normally when comskip is off."""
        result = await self._apply(
            initial={"comskip_enabled": False, "transcode_enabled": True},
            update={"transcode_enabled": False},
        )
        self.assertFalse(result["comskip_enabled"])
        self.assertFalse(result["transcode_enabled"])

    async def test_remux_can_be_enabled_when_comskip_is_off(self):
        """remux_only=True is accepted normally when comskip is off."""
        result = await self._apply(
            initial={"comskip_enabled": False, "remux_only": False},
            update={"remux_only": True},
        )
        self.assertFalse(result["comskip_enabled"])
        self.assertTrue(result["remux_only"])


if __name__ == "__main__":
    unittest.main()
