"""The recording event bus in isolation: envelope, dispatch, isolation, drops.

The pipeline-level tests (that a real download publishes started then
completed) live in ``test_recording_event_seam.py``. This file only covers the
dispatch module itself.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.recording_events import (  # noqa: E402
    EVENT_CONTRACT_VERSION,
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    FakeRecordingSubscriber,
    RecordingEventBus,
)


class RecordingEventBusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = RecordingEventBus()

    async def test_publish_delivers_to_subscriber(self):
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        self.bus.publish(RECORDING_STARTED, {"id": 7, "title": "Match of the Day"})
        await self.bus.drain_once()

        self.assertEqual(sub.names, [RECORDING_STARTED])
        self.assertEqual(sub.events[0].recording["title"], "Match of the Day")

    async def test_envelope_carries_version_id_and_timestamp(self):
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        self.bus.publish(RECORDING_COMPLETED, {"id": 1})
        self.bus.publish(RECORDING_COMPLETED, {"id": 2})
        await self.bus.drain_once()

        first, second = sub.events
        self.assertEqual(first.version, EVENT_CONTRACT_VERSION)
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertIsNotNone(first.occurred_at.tzinfo)
        self.assertEqual(first.to_dict()["event"], RECORDING_COMPLETED)

    async def test_publish_returns_before_subscriber_runs(self):
        """Publish hands off to a queue; it does not await the subscriber."""
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        self.bus.publish(RECORDING_STARTED, {"id": 1})
        self.assertEqual(sub.events, [])
        self.assertEqual(self.bus.pending, 1)

    async def test_raising_subscriber_does_not_stop_the_next_one(self):
        boom = FakeRecordingSubscriber(raise_on=RECORDING_COMPLETED)
        good = FakeRecordingSubscriber()
        self.bus.subscribe(boom)
        self.bus.subscribe(good)

        self.bus.publish(RECORDING_COMPLETED, {"id": 3})
        await self.bus.drain_once()

        self.assertEqual(good.names, [RECORDING_COMPLETED])

    async def test_raising_subscriber_does_not_raise_out_of_the_bus(self):
        self.bus.subscribe(FakeRecordingSubscriber(raise_on=RECORDING_FAILED))
        self.bus.publish(RECORDING_FAILED, {"id": 4})
        await self.bus.drain_once()  # must not raise

    async def test_blocking_subscriber_does_not_block_publish(self):
        blocker = FakeRecordingSubscriber(block_on=RECORDING_STARTED)
        self.bus.subscribe(blocker)
        task = asyncio.create_task(self.bus.run())
        try:
            for index in range(5):
                self.bus.publish(RECORDING_STARTED, {"id": index})
            # publish never awaited the wedged subscriber
            await asyncio.sleep(0)
            self.assertEqual(blocker.events, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_events_arrive_in_publication_order(self):
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        self.bus.publish(RECORDING_FAILED, {"id": 9, "retry_count": 0})
        self.bus.publish(RECORDING_STARTED, {"id": 9, "retry_count": 1})
        self.bus.publish(RECORDING_COMPLETED, {"id": 9, "retry_count": 1})
        await self.bus.drain_once()

        self.assertEqual(
            sub.names, [RECORDING_FAILED, RECORDING_STARTED, RECORDING_COMPLETED]
        )

    async def test_full_queue_drops_rather_than_blocking(self):
        bus = RecordingEventBus(max_queue_size=2)
        sub = FakeRecordingSubscriber()
        bus.subscribe(sub)

        self.assertIsNotNone(bus.publish(RECORDING_STARTED, {"id": 1}))
        self.assertIsNotNone(bus.publish(RECORDING_STARTED, {"id": 2}))
        self.assertIsNone(bus.publish(RECORDING_STARTED, {"id": 3}))
        self.assertEqual(bus.dropped_events, 1)

        await bus.drain_once()
        self.assertEqual([e.recording["id"] for e in sub.events], [1, 2])

    async def test_build_does_not_publish(self):
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        event = self.bus.build(RECORDING_COMPLETED, {"id": 11})
        await self.bus.drain_once()
        self.assertEqual(sub.events, [])

        self.bus.publish_event(event)
        await self.bus.drain_once()
        self.assertEqual(sub.events, [event])

    async def test_payload_is_copied_at_publish_time(self):
        """A later mutation of the dict cannot rewrite a published event."""
        sub = FakeRecordingSubscriber()
        self.bus.subscribe(sub)

        payload = {"id": 12, "status": "downloading"}
        self.bus.publish(RECORDING_STARTED, payload)
        payload["status"] = "completed"
        await self.bus.drain_once()

        self.assertEqual(sub.events[0].recording["status"], "downloading")

    async def test_sync_subscriber_is_supported(self):
        received = []
        self.bus.subscribe(lambda event: received.append(event.name))

        self.bus.publish(RECORDING_STARTED, {"id": 13})
        await self.bus.drain_once()

        self.assertEqual(received, [RECORDING_STARTED])


if __name__ == "__main__":
    unittest.main()
