"""The webhook consumer is actually plugged into the app's event bus.

Two promises live here: registering once puts a webhook subscriber on the
app-wide bus, and the subscriber reads the *current* settings row on every
event, so saving a URL in Settings takes effect without a restart.
"""

import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from database import Base  # noqa: E402
from models.settings import AppSettings  # noqa: E402
from services import webhook_dispatcher as dispatcher_module  # noqa: E402
from services.recording_events import (  # noqa: E402
    RECORDING_COMPLETED,
    RecordingEventBus,
)
from services.webhook_dispatcher import (  # noqa: E402
    FakeWebhookSender,
    WebhookDispatcher,
    load_webhook_targets,
    register_webhook_subscriber,
)


class RegistrationTests(unittest.TestCase):
    def test_register_adds_one_subscriber_to_the_bus(self):
        bus = RecordingEventBus()

        register_webhook_subscriber(bus)

        self.assertEqual(bus.subscriber_count, 1)


class LoadTargetsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        self._real_maker = dispatcher_module.async_session_maker
        dispatcher_module.async_session_maker = self.Session

    async def asyncTearDown(self):
        dispatcher_module.async_session_maker = self._real_maker
        await self.engine.dispose()

    async def test_no_settings_row_yet_means_no_webhooks(self):
        self.assertEqual(await load_webhook_targets(), {})

    async def test_targets_come_from_the_settings_row(self):
        async with self.Session() as session:
            session.add(
                AppSettings(webhook_url_recording_completed="https://ntfy.sh/topic")
            )
            await session.commit()

        targets = await load_webhook_targets()

        self.assertEqual(
            targets["webhook_url_recording_completed"], "https://ntfy.sh/topic"
        )

    async def test_a_settings_change_is_picked_up_without_a_restart(self):
        sender = FakeWebhookSender()
        subscriber = WebhookDispatcher(load_targets=load_webhook_targets, sender=sender)
        bus = RecordingEventBus()
        bus.subscribe(subscriber)

        bus.publish(RECORDING_COMPLETED, {"id": 1})
        await bus.drain_once()
        self.assertEqual(sender.calls, [])

        async with self.Session() as session:
            session.add(
                AppSettings(webhook_url_recording_completed="https://ntfy.sh/later")
            )
            await session.commit()

        bus.publish(RECORDING_COMPLETED, {"id": 2})
        await bus.drain_once()

        self.assertEqual(sender.urls, ["https://ntfy.sh/later"])


if __name__ == "__main__":
    unittest.main()
