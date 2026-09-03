"""Webhook URLs as app settings: stored, returned, validated, clearable.

The settings router is called directly, the way the other settings tests do
it (see ``test_comskip_tunables_api.py``) — no TestClient, so a 400 shows up
as ``HTTPException`` and a schema rejection as ``ValidationError``.
"""

import os
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.settings import SettingsUpdate, get_settings, update_settings  # noqa: E402
from database import Base  # noqa: E402
from models.settings import AppSettings  # noqa: E402
from services.webhook_dispatcher import WEBHOOK_SETTING_FIELDS  # noqa: E402


class WebhookSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_new_install_has_every_webhook_off(self):
        async with self.Session() as session:
            settings = AppSettings()
            session.add(settings)
            await session.commit()
            payload = settings.to_dict()

        for field in WEBHOOK_SETTING_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, payload)
                self.assertEqual(payload[field], "")

    async def test_saving_a_url_round_trips_through_get(self):
        async with self.Session() as session:
            await update_settings(
                SettingsUpdate(webhook_url_recording_completed="https://ntfy.sh/mustarrd"),
                None,
                session,
            )
            fetched = await get_settings(None, session)

        self.assertEqual(
            fetched["webhook_url_recording_completed"], "https://ntfy.sh/mustarrd"
        )

    async def test_a_lan_target_is_accepted(self):
        async with self.Session() as session:
            result = await update_settings(
                SettingsUpdate(
                    webhook_url_postprocessing_completed="http://192.168.1.50:32400/library/sections/1/refresh"
                ),
                None,
                session,
            )

        self.assertEqual(
            result["webhook_url_postprocessing_completed"],
            "http://192.168.1.50:32400/library/sections/1/refresh",
        )

    async def test_url_is_trimmed_on_save(self):
        async with self.Session() as session:
            result = await update_settings(
                SettingsUpdate(webhook_url_recording_started="  https://ntfy.sh/a  "),
                None,
                session,
            )

        self.assertEqual(result["webhook_url_recording_started"], "https://ntfy.sh/a")

    async def test_clearing_the_field_turns_the_webhook_off(self):
        async with self.Session() as session:
            await update_settings(
                SettingsUpdate(webhook_url_recording_failed="https://ntfy.sh/a"),
                None,
                session,
            )
            result = await update_settings(
                SettingsUpdate(webhook_url_recording_failed=""), None, session
            )

        self.assertEqual(result["webhook_url_recording_failed"], "")

    async def test_a_bad_scheme_is_rejected_at_save_time(self):
        for bad in ("file:///etc/passwd", "ftp://host/x", "not a url"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    SettingsUpdate(webhook_url_recording_completed=bad)

    async def test_the_cloud_metadata_address_is_rejected(self):
        with self.assertRaises(ValidationError):
            SettingsUpdate(
                webhook_url_recording_completed="http://169.254.169.254/latest/meta-data/"
            )

    async def test_webhook_urls_are_not_in_the_public_settings_response(self):
        from types import SimpleNamespace

        from api.settings import get_public_settings

        auth = SimpleNamespace(is_admin=True, user=None)
        async with self.Session() as session:
            await update_settings(
                SettingsUpdate(webhook_url_recording_completed="https://ntfy.sh/secret-token"),
                None,
                session,
            )
            public = await get_public_settings(auth, session)

        for field in WEBHOOK_SETTING_FIELDS:
            self.assertNotIn(field, public)
        self.assertNotIn("secret-token", str(public))


if __name__ == "__main__":
    unittest.main()
