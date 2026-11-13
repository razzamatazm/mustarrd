"""One seam for announcing what happened to a recording.

"A recording completed" used to be a couple of dozen assignments to
``Download.status`` scattered through the download manager. Anything that
wanted to react — a webhook, a notification, a library refresh — had to find
and correctly edit every one of them.

This module is the announcement side of the fix. The download manager's
state-transition helpers are the only writers of ``Download.status``, and each
of them publishes here after its commit succeeds. Subscribers registered at
startup hear about every transition without anyone remembering to tell them.

Shape:

- ``RecordingEventBus.publish`` is fire-and-forget. It stamps an envelope,
  drops it on a bounded queue and returns. The pipeline never awaits a
  subscriber, so a slow or wedged webhook cannot stall the download queue.
- ``RecordingEventBus.run`` is the drain task, started in the app lifespan
  alongside the other managers. One task consuming one queue means events for
  a recording arrive in the order they happened.
- A subscriber that raises is logged and skipped. Other subscribers for the
  same event still run, and nothing propagates back to the recording.
- Delivery is not durable. A full queue drops and logs; a restart loses
  anything undelivered. Consumers persist on receipt.

``FakeRecordingSubscriber`` is the test adapter, in the mould of
``FakeProcessRunner``: hand it to ``subscribe()``, run the path, assert on the
list of events that arrived.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Bumped when the envelope or payload shape changes incompatibly. Consumers
# (#431 webhooks, #430 notifications) are written against a version.
EVENT_CONTRACT_VERSION = 1

RECORDING_STARTED = "recording.started"
RECORDING_COMPLETED = "recording.completed"
RECORDING_FAILED = "recording.failed"
RECORDING_CANCELLED = "recording.cancelled"
POSTPROCESSING_COMPLETED = "postprocessing.completed"

DEFAULT_QUEUE_SIZE = 1000


@dataclass(frozen=True)
class RecordingEvent:
    """One thing that happened to one recording.

    ``recording`` is built from ``Download.to_dict()``, which deliberately
    omits ``source_url`` — the provider URL embeds account credentials and
    must never reach a subscriber that may forward it off the box.
    """

    name: str
    recording: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = EVENT_CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.name,
            "version": self.version,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at.isoformat(),
            "recording": self.recording,
        }


Subscriber = Callable[[RecordingEvent], Optional[Awaitable[None]]]


class RecordingEventBus:
    """In-process publish/subscribe for recording lifecycle events."""

    def __init__(self, max_queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._queue: "asyncio.Queue[RecordingEvent]" = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._subscribers: List[Subscriber] = []
        self.dropped_events = 0

    # --- registration -----------------------------------------------------
    def subscribe(self, subscriber: Subscriber) -> None:
        """Register a callable (sync or async) to receive every event."""
        self._subscribers.append(subscriber)

    def clear_subscribers(self) -> None:
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # --- publishing -------------------------------------------------------
    def build(self, name: str, recording: Dict[str, Any]) -> RecordingEvent:
        """Stamp an envelope without publishing it.

        Deferred transitions (restart recovery batches many status writes
        behind one commit) build their events as they go and publish the whole
        batch once the commit succeeds — or discard it if the commit fails.
        """
        return RecordingEvent(name=name, recording=dict(recording or {}))

    def publish_event(self, event: RecordingEvent) -> bool:
        """Enqueue a pre-built envelope. Returns False if it was dropped."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            logger.warning(
                "Recording event queue full (%d); dropped %s for download %s",
                self._max_queue_size,
                event.name,
                (event.recording or {}).get("id"),
            )
            return False
        return True

    def publish(self, name: str, recording: Dict[str, Any]) -> Optional[RecordingEvent]:
        """Announce something that happened. Never blocks, never raises."""
        event = self.build(name, recording)
        return event if self.publish_event(event) else None

    # --- draining ---------------------------------------------------------
    async def _deliver(self, event: RecordingEvent) -> None:
        for subscriber in list(self._subscribers):
            try:
                result = subscriber(event)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a subscriber is not the recording's problem
                logger.error(
                    "Recording event subscriber %r failed handling %s: %s",
                    getattr(subscriber, "__name__", subscriber),
                    event.name,
                    exc,
                    exc_info=True,
                )

    async def run(self) -> None:
        """Drain the queue forever. Started as a background task at startup."""
        logger.info("Recording event dispatcher started")
        while True:
            event = await self._queue.get()
            try:
                await self._deliver(event)
            finally:
                self._queue.task_done()

    async def drain_once(self) -> None:
        """Deliver everything currently queued. For tests and shutdown."""
        while not self._queue.empty():
            event = self._queue.get_nowait()
            try:
                await self._deliver(event)
            finally:
                self._queue.task_done()

    @property
    def pending(self) -> int:
        return self._queue.qsize()


# The app-wide bus. The download manager publishes here; startup registers
# subscribers and starts ``run()``.
recording_events = RecordingEventBus()


class FakeRecordingSubscriber:
    """Test adapter: records the events it received.

    Construct, hand to ``subscribe()``, run the path, assert on ``names`` or
    ``events``. ``raise_on`` makes it blow up for a given event name so
    isolation can be tested; ``block_on`` makes it hang forever.
    """

    def __init__(
        self,
        raise_on: Optional[str] = None,
        block_on: Optional[str] = None,
    ) -> None:
        self.events: List[RecordingEvent] = []
        self.raise_on = raise_on
        self.block_on = block_on

    async def __call__(self, event: RecordingEvent) -> None:
        if self.block_on is not None and event.name == self.block_on:
            await asyncio.Event().wait()
        self.events.append(event)
        if self.raise_on is not None and event.name == self.raise_on:
            raise RuntimeError(f"subscriber blew up on {event.name}")

    @property
    def names(self) -> List[str]:
        return [event.name for event in self.events]

    def of(self, name: str) -> List[RecordingEvent]:
        return [event for event in self.events if event.name == name]

