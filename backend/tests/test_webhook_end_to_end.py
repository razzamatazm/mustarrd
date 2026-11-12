"""A real recording finishing fires the configured webhook, and a broken
webhook does not touch the recording.

This drives the actual download pipeline (the harness from
``test_recording_event_seam.py``) rather than hand-publishing an event, so it
covers the whole chain: the manager's transition helper, the seam, the webhook
subscriber, and the settings row the URL came from.
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from database import Base  # noqa: E402
from models import AppSettings, Download, DownloadStatus  # noqa: E402
from services import webhook_dispatcher as dispatcher_module  # noqa: E402
from services.download_manager import DownloadManager  # noqa: E402
from services.recording_events import (  # noqa: E402
    RECORDING_COMPLETED,
    RECORDING_STARTED,
    RecordingEventBus,
)
from services.webhook_dispatcher import (  # noqa: E402
    FakeWebhookSender,
    WebhookDispatcher,
    load_webhook_targets,
)


class WebhookEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.tmpdir = tempfile.mkdtemp()
        self.download_folder = os.path.join(self.tmpdir, "downloads")
        self.completed_folder = os.path.join(self.tmpdir, "completed")
        os.makedirs(self.download_folder, exist_ok=True)
        os.makedirs(self.completed_folder, exist_ok=True)

        self.bus = RecordingEventBus()
        self.sender = FakeWebhookSender()
        self.bus.subscribe(
            WebhookDispatcher(load_targets=load_webhook_targets, sender=self.sender)
        )

        self._real_maker = dispatcher_module.async_session_maker
        dispatcher_module.async_session_maker = self.session_factory

    async def asyncTearDown(self):
        dispatcher_module.async_session_maker = self._real_maker
        await self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @contextlib.contextmanager
    def _patched(self, **extra):
        @contextlib.asynccontextmanager
        async def session_maker():
            async with self.session_factory() as session:
                yield session

        patches = [
            patch("services.download_manager.async_session_maker", session_maker),
            patch("services.download_manager.recording_events", self.bus),
            patch.object(self.manager, "_broadcast_progress", new_callable=AsyncMock),
            patch.object(self.manager, "_broadcast_log", new_callable=AsyncMock),
            patch.object(self.manager, "_trigger_plex_refresh", new_callable=AsyncMock),
            patch.object(self.manager, "_store_recorded_duration", new_callable=AsyncMock),
            patch.object(self.manager, "_write_nfo_sidecar", new_callable=AsyncMock),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            for target, replacement in extra.items():
                stack.enter_context(patch.object(self.manager, target, replacement))
            yield

    async def _seed(self, **webhook_urls) -> int:
        async with self.session_factory() as session:
            session.add(
                AppSettings(
                    download_folder=self.download_folder,
                    completed_folder=self.completed_folder,
                    min_free_space_gb=0,
                    **webhook_urls,
                )
            )
            download = Download(
                account_id=1,
                channel_id="1",
                channel_name="Test Channel",
                program_title="Test Show",
                program_start=datetime(2024, 1, 1, 20, 0, 0),
                program_end=datetime(2024, 1, 1, 21, 0, 0),
                duration_minutes=60,
                source_url="http://provider.test/live/user/pass/1.ts",
                output_path=os.path.join(self.download_folder, "show.ts"),
                status=DownloadStatus.PENDING.value,
                progress=0.0,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id

    async def _run_download(self, download_id):
        os.makedirs(self.download_folder, exist_ok=True)
        with open(os.path.join(self.download_folder, "show.ts"), "wb") as handle:
            handle.write(b"payload")

        async def fake_download(*args, **kwargs):
            return 1024

        with self._patched(
            _download_catchup_stream=AsyncMock(side_effect=fake_download),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

    async def test_a_completed_recording_posts_to_the_configured_webhook(self):
        download_id = await self._seed(
            webhook_url_recording_completed="https://ntfy.sh/mustarrd"
        )

        await self._run_download(download_id)

        self.assertEqual(self.sender.urls, ["https://ntfy.sh/mustarrd"])
        _, payload = self.sender.calls[0]
        self.assertEqual(payload["event"], RECORDING_COMPLETED)
        self.assertEqual(payload["recording"]["program_title"], "Test Show")
        self.assertEqual(payload["recording"]["channel_name"], "Test Channel")
        self.assertEqual(payload["recording"]["status"], DownloadStatus.COMPLETED.value)
        self.assertTrue(
            payload["recording"]["output_path"].startswith(self.completed_folder)
        )
        self.assertIn("program_start", payload["recording"])
        self.assertIn("program_end", payload["recording"])
        self.assertIn("error_message", payload["recording"])

    async def test_the_payload_never_carries_provider_credentials(self):
        download_id = await self._seed(
            webhook_url_recording_started="https://ntfy.sh/mustarrd"
        )

        await self._run_download(download_id)

        body = str(self.sender.calls[0][1])
        self.assertNotIn("source_url", body)
        self.assertNotIn("pass", body)

    async def test_only_the_events_with_a_url_are_sent(self):
        download_id = await self._seed(
            webhook_url_recording_started="https://ntfy.sh/started"
        )

        await self._run_download(download_id)

        self.assertEqual(self.sender.urls, ["https://ntfy.sh/started"])
        self.assertEqual(self.sender.calls[0][1]["event"], RECORDING_STARTED)

    async def test_a_failing_webhook_leaves_the_recording_completed(self):
        self.sender.raise_on = "https://ntfy.sh/broken"
        download_id = await self._seed(
            webhook_url_recording_completed="https://ntfy.sh/broken"
        )

        with self.assertLogs("services.webhook_dispatcher", level="WARNING"):
            await self._run_download(download_id)

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            download = result.scalar_one()
        self.assertEqual(download.status, DownloadStatus.COMPLETED.value)
        self.assertIsNone(download.error_message)

    async def test_no_webhook_configured_sends_nothing(self):
        download_id = await self._seed()

        await self._run_download(download_id)

        self.assertEqual(self.sender.calls, [])


if __name__ == "__main__":
    unittest.main()
