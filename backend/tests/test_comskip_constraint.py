"""
Regression tests for ComSkip settings constraints.

Bug 1 (HIGH): update_settings only enforced "comskip_enabled=True forces
transcode_enabled=True" when the current request contained comskip_enabled.
A second request setting transcode_enabled=False bypassed the constraint,
leaving comskip_enabled=True + transcode_enabled=False in the DB. ComSkip
ran and produced an EDL that was silently ignored; commercials stayed in
the output with no user-visible error.

Fix: transcode_enabled=True is enforced on the final stored state after all
field updates.  remux_only is intentionally NOT constrained: the pipeline
supports stream-copy commercial removal (segment extraction + concat -c copy),
so remux_only=True + comskip_enabled=True is the valid fast "remux + skip
commercials" profile written by the Docker onboarding flow.
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

    async def test_remux_allowed_with_comskip_on(self):
        """PUT remux_only=True is accepted when comskip_enabled is already True.

        The pipeline supports stream-copy commercial removal: segment extraction
        runs with -c copy, then concat with -c copy.  remux_only=True +
        comskip_enabled=True is the fast "remux + skip commercials" profile that
        Docker onboarding writes and that users can select explicitly.
        """
        result = await self._apply(
            initial={"comskip_enabled": True, "remux_only": False},
            update={"remux_only": True},
        )
        self.assertTrue(result["remux_only"],
            "remux_only=True must be accepted when comskip_enabled is already True")
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

    async def test_enabling_comskip_preserves_remux_state(self):
        """Enabling comskip does not change remux_only; both True and False are valid."""
        result = await self._apply(
            initial={"comskip_enabled": False, "remux_only": True},
            update={"comskip_enabled": True},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertTrue(result["remux_only"],
            "enabling comskip must not clear remux_only; fast remux+comskip is a supported profile")

    async def test_enabling_comskip_with_conflicting_transcode_in_same_request(self):
        """comskip_enabled=True + transcode_enabled=False in the same request: comskip wins for transcode."""
        result = await self._apply(
            initial={"comskip_enabled": False, "transcode_enabled": True},
            update={"comskip_enabled": True, "transcode_enabled": False, "remux_only": True},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertTrue(result["transcode_enabled"],
            "comskip constraint must win over explicit transcode_enabled=False in same request")
        self.assertTrue(result["remux_only"],
            "remux_only=True is valid alongside comskip_enabled=True; it is not overridden")

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

    async def test_remux_comskip_profile_survives_settings_save(self):
        """remux_only=True + comskip_enabled=True is preserved on any settings save.

        Regression: before this fix, any PUT /settings call while this profile
        was active forced remux_only=False, silently downgrading the user from
        fast stream-copy commercial removal to full re-encode.  Docker onboarding
        writes this exact profile (remux_comskip) as the recommended path.
        """
        result = await self._apply(
            initial={"comskip_enabled": True, "remux_only": True, "transcode_enabled": True},
            update={"comskip_path": "/usr/bin/comskip"},
        )
        self.assertTrue(result["comskip_enabled"])
        self.assertTrue(result["remux_only"],
            "remux_only must survive a settings save; silently clobbering it "
            "would downgrade Docker onboarding users from fast remux to full re-encode")
        self.assertTrue(result["transcode_enabled"])


if __name__ == "__main__":
    unittest.main()
