"""The recording lifecycle seam, driven through real pipeline paths.

These tests run the download manager against an in-memory SQLite database (the
harness from test_dispatch_rollback_expiry / test_commit_failure_after_move)
and assert on what a fake subscriber received. They never count status
assignments or check which helper was called: the whole point of the seam is
that the number of call sites stops mattering, and a test that counts them
re-creates the problem it was meant to remove.
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
from services.download_manager import DownloadManager  # noqa: E402
from services.recording_events import (  # noqa: E402
    POSTPROCESSING_COMPLETED,
    RECORDING_CANCELLED,
    RECORDING_COMPLETED,
    RECORDING_FAILED,
    RECORDING_STARTED,
    FakeRecordingSubscriber,
    RecordingEventBus,
)


class RecordingEventSeamTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # expire_on_commit=False matches the production session factory.
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.manager = DownloadManager()
        self.tmpdir = tempfile.mkdtemp()
        self.download_folder = os.path.join(self.tmpdir, "downloads")
        self.completed_folder = os.path.join(self.tmpdir, "completed")
        os.makedirs(self.download_folder, exist_ok=True)
        os.makedirs(self.completed_folder, exist_ok=True)

        self.bus = RecordingEventBus()
        self.subscriber = FakeRecordingSubscriber()
        self.bus.subscribe(self.subscriber)

    async def asyncTearDown(self):
        await self.engine.dispose()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- harness ---------------------------------------------------------
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

    async def _seed(self, **overrides) -> int:
        output_path = overrides.pop(
            "output_path", os.path.join(self.download_folder, "show.ts")
        )
        status = overrides.pop("status", DownloadStatus.PENDING.value)
        async with self.session_factory() as session:
            existing = await session.execute(select(AppSettings))
            if existing.scalars().first() is None:
                session.add(
                    AppSettings(
                        download_folder=self.download_folder,
                        completed_folder=self.completed_folder,
                        min_free_space_gb=0,
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
                output_path=output_path,
                status=status,
                progress=0.0,
                **overrides,
            )
            session.add(download)
            await session.commit()
            await session.refresh(download)
            return download.id

    def _write_file(self, path: str, content: bytes = b"payload") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    async def _status(self, download_id: int) -> str:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            return result.scalar_one().status

    # --- happy path ------------------------------------------------------
    async def test_plain_download_publishes_started_then_completed(self):
        download_id = await self._seed()
        source = self._write_file(os.path.join(self.download_folder, "show.ts"))

        async def fake_download(*args, **kwargs):
            return 1024

        with self._patched(
            _download_catchup_stream=AsyncMock(side_effect=fake_download),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(
            self.subscriber.names, [RECORDING_STARTED, RECORDING_COMPLETED]
        )
        completed = self.subscriber.of(RECORDING_COMPLETED)[0].recording
        self.assertEqual(completed["status"], DownloadStatus.COMPLETED.value)
        self.assertTrue(
            completed["output_path"].startswith(self.completed_folder),
            completed["output_path"],
        )
        self.assertFalse(os.path.exists(source))
        self.assertFalse(completed["recovered"])
        self.assertFalse(completed["post_processed"])
        self.assertIsNone(completed["warning"])

    async def test_event_payload_never_carries_the_source_url(self):
        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertTrue(self.subscriber.events)
        for event in self.subscriber.events:
            self.assertNotIn("source_url", event.recording)
            self.assertNotIn("pass", str(event.recording))

    async def test_started_event_carries_retry_count(self):
        download_id = await self._seed(retry_count=2)
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        for event in self.subscriber.events:
            self.assertEqual(event.recording["retry_count"], 2)

    async def test_completion_with_warnings_is_a_completion(self):
        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value="file may be corrupt"),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertIn(RECORDING_COMPLETED, self.subscriber.names)
        self.assertNotIn(RECORDING_FAILED, self.subscriber.names)
        payload = self.subscriber.of(RECORDING_COMPLETED)[0].recording
        self.assertEqual(payload["warning"], "file may be corrupt")
        self.assertEqual(
            payload["error_message"], "Completed with warnings: file may be corrupt"
        )

    # --- failure ---------------------------------------------------------
    async def test_failed_download_publishes_failed_and_no_completion(self):
        download_id = await self._seed()

        with self._patched(
            _download_catchup_stream=AsyncMock(side_effect=Exception("provider gone")),
            _partial_is_playable=AsyncMock(return_value=False),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.names, [RECORDING_STARTED, RECORDING_FAILED])
        failed = self.subscriber.of(RECORDING_FAILED)[0].recording
        self.assertEqual(failed["status"], DownloadStatus.FAILED.value)
        self.assertIn("provider gone", failed["error_message"])

    async def test_failed_then_retried_publishes_both_in_order(self):
        download_id = await self._seed()

        with self._patched(
            _download_catchup_stream=AsyncMock(side_effect=Exception("provider gone")),
            _partial_is_playable=AsyncMock(return_value=False),
        ):
            await self.manager._execute_download(download_id)

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            # What the scheduler's auto-retry does before it requeues.
            result.scalar_one().retry_count = 1
            await session.commit()

        with self._patched():
            await self.manager.retry_download(download_id)

        self._write_file(os.path.join(self.download_folder, "show.ts"))
        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(
            self.subscriber.names,
            [RECORDING_STARTED, RECORDING_FAILED, RECORDING_STARTED, RECORDING_COMPLETED],
        )
        self.assertEqual(
            [e.recording["retry_count"] for e in self.subscriber.events],
            [0, 0, 1, 1],
        )

    # --- rollback --------------------------------------------------------
    async def test_a_commit_that_fails_at_completion_publishes_nothing(self):
        """The rollback lie: an event must never describe a write that failed.

        The completion commit is poisoned and the salvage path that would
        otherwise persist it on a fresh session is stubbed out, so nothing
        reaches the database and nothing may be announced.
        """
        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        calls = {"n": 0}

        async def poison_first_commit(session, did, status):
            calls["n"] += 1
            if calls["n"] == 1:
                obj = await session.get(Download, did)
                # NOT NULL column: the commit right after this fails.
                obj.source_url = None

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
            _sync_schedule_status=AsyncMock(side_effect=poison_first_commit),
            _finalize_completed_after_interrupt=AsyncMock(),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.names, [RECORDING_STARTED])
        self.assertNotEqual(
            await self._status(download_id), DownloadStatus.COMPLETED.value
        )

    async def test_the_salvage_retry_announces_the_completion_exactly_once(self):
        """A poisoned commit that the fresh-session retry rescues announces once."""
        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        calls = {"n": 0}

        async def poison_first_commit(session, did, status):
            calls["n"] += 1
            if calls["n"] == 1:
                obj = await session.get(Download, did)
                obj.source_url = None

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
            _sync_schedule_status=AsyncMock(side_effect=poison_first_commit),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.names.count(RECORDING_COMPLETED), 1)
        self.assertEqual(await self._status(download_id), DownloadStatus.COMPLETED.value)

    async def test_a_rolled_back_completion_publishes_nothing(self):
        """The status write happened, the commit did not: silence."""
        download_id = await self._seed()

        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.id == download_id)
            )
            download = result.scalar_one()
            with patch("services.download_manager.recording_events", self.bus):
                with patch.object(
                    session, "commit", side_effect=RuntimeError("disk gone")
                ):
                    with self.assertRaises(RuntimeError):
                        await self.manager._transition_completed(
                            session,
                            download,
                            status=DownloadStatus.COMPLETED.value,
                        )
                await session.rollback()
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.events, [])
        self.assertEqual(await self._status(download_id), DownloadStatus.PENDING.value)

    async def test_finalize_after_interrupt_publishes_nothing_when_both_writes_fail(self):
        """The pre-existing optimistic broadcast must not become an optimistic event."""
        download_id = await self._seed()

        with self._patched():
            with patch.object(
                self.manager,
                "_sync_schedule_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("database gone"),
            ):
                await self.manager._finalize_completed_after_interrupt(
                    await self._open_session(),
                    download_id,
                    os.path.join(self.completed_folder, "show.ts"),
                )
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.events, [])

    async def _open_session(self):
        session = self.session_factory()
        self.addAsyncCleanup(session.close)
        return await session.__aenter__()

    # --- cancellation ----------------------------------------------------
    async def test_cancelling_a_pending_download_publishes_cancelled_only(self):
        download_id = await self._seed()

        with self._patched():
            await self.manager.cancel_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.names, [RECORDING_CANCELLED])
        self.assertNotIn(RECORDING_COMPLETED, self.subscriber.names)
        self.assertNotIn(RECORDING_FAILED, self.subscriber.names)
        self.assertEqual(await self._status(download_id), DownloadStatus.CANCELLED.value)

    # --- post-processing --------------------------------------------------
    async def test_post_processing_publishes_postprocessing_then_completed(self):
        source = os.path.join(self.download_folder, "show.ts")
        download_id = await self._seed(
            output_path=source, status=DownloadStatus.PROCESSING.value
        )
        self._write_file(source)
        processed = self._write_file(os.path.join(self.download_folder, "show.mkv"))

        async def fake_post_process(original_path, did, session, settings):
            return processed, []

        with self._patched(
            _needs_post_processing=lambda *a, **k: True,
            _post_process=AsyncMock(side_effect=fake_post_process),
            _integrity_check_warning=AsyncMock(return_value=None),
            _cleanup_working_files=lambda *a, **k: None,
        ):
            await self.manager._execute_post_process(download_id)
        await self.bus.drain_once()

        self.assertEqual(
            self.subscriber.names, [POSTPROCESSING_COMPLETED, RECORDING_COMPLETED]
        )
        first, second = self.subscriber.events
        self.assertTrue(first.recording["post_processed"])
        self.assertEqual(second.recording["status"], DownloadStatus.COMPLETED.value)

    async def test_finalizing_without_post_processing_does_not_announce_it(self):
        source = os.path.join(self.download_folder, "show.ts")
        download_id = await self._seed(
            output_path=source, status=DownloadStatus.PROCESSING.value
        )
        self._write_file(source)

        with self._patched(
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_post_process(download_id)
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.names, [RECORDING_COMPLETED])

    # --- restart recovery -------------------------------------------------
    async def test_recovery_finalising_publishes_completion_tagged_recovered(self):
        source = os.path.join(self.completed_folder, "show.ts")
        download_id = await self._seed(
            output_path=source, status=DownloadStatus.PROCESSING.value
        )
        self._write_file(source)

        with self._patched(_needs_post_processing=lambda *a, **k: False):
            recovered = await self.manager.recover_incomplete_downloads()
        await self.bus.drain_once()

        self.assertEqual(recovered, 0)
        self.assertEqual(self.subscriber.names, [RECORDING_COMPLETED])
        payload = self.subscriber.events[0].recording
        self.assertTrue(payload["recovered"])
        self.assertEqual(payload["status"], DownloadStatus.COMPLETED.value)

    async def test_recovery_that_fails_to_commit_publishes_nothing(self):
        source = os.path.join(self.completed_folder, "show.ts")
        download_id = await self._seed(
            output_path=source, status=DownloadStatus.PROCESSING.value
        )
        self._write_file(source)

        async def poison(session, did, status):
            obj = await session.get(Download, did)
            obj.source_url = None  # NOT NULL: the batch commit fails

        with self._patched(
            _needs_post_processing=lambda *a, **k: False,
            _sync_schedule_status=AsyncMock(side_effect=poison),
        ):
            with self.assertRaises(Exception):
                await self.manager.recover_incomplete_downloads()
        await self.bus.drain_once()

        self.assertEqual(self.subscriber.events, [])

    async def test_a_requeued_recording_starts_tagged_as_recovered(self):
        source = os.path.join(self.download_folder, "show.ts")
        download_id = await self._seed(
            output_path=source, status=DownloadStatus.DOWNLOADING.value
        )

        with self._patched(_needs_post_processing=lambda *a, **k: False):
            await self.manager.recover_incomplete_downloads()
        self._write_file(source)
        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        started = self.subscriber.of(RECORDING_STARTED)
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].recording["recovered"])
        # The tag is consumed: a later run of the same recording is not
        # recovery.
        self.assertFalse(self.subscriber.of(RECORDING_COMPLETED)[0].recording["recovered"])

    # --- subscriber isolation ---------------------------------------------
    async def test_a_raising_subscriber_cannot_fail_a_recording(self):
        self.bus.clear_subscribers()
        boom = FakeRecordingSubscriber(raise_on=RECORDING_COMPLETED)
        good = FakeRecordingSubscriber()
        self.bus.subscribe(boom)
        self.bus.subscribe(good)

        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(await self._status(download_id), DownloadStatus.COMPLETED.value)
        self.assertIn(RECORDING_COMPLETED, good.names)

    async def test_a_blocked_subscriber_does_not_delay_the_recording(self):
        self.bus.clear_subscribers()
        self.bus.subscribe(FakeRecordingSubscriber(block_on=RECORDING_STARTED))

        download_id = await self._seed()
        self._write_file(os.path.join(self.download_folder, "show.ts"))

        second_id = await self._seed(
            output_path=os.path.join(self.download_folder, "other.ts")
        )
        self._write_file(os.path.join(self.download_folder, "other.ts"))

        with self._patched(
            _download_catchup_stream=AsyncMock(return_value=1024),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
            # The queue keeps moving: the next recording runs to completion
            # with the first one's subscriber still wedged.
            await self.manager._execute_download(second_id)

        # No drain: both finished without any subscriber running.
        self.assertEqual(await self._status(download_id), DownloadStatus.COMPLETED.value)
        self.assertEqual(await self._status(second_id), DownloadStatus.COMPLETED.value)

    # --- interrupted ------------------------------------------------------
    async def test_an_interrupted_recording_publishes_a_completion(self):
        source = os.path.join(self.download_folder, "show.ts")
        download_id = await self._seed(output_path=source)
        self._write_file(source)

        with self._patched(
            _download_catchup_stream=AsyncMock(side_effect=Exception("stream ended")),
            _partial_is_playable=AsyncMock(return_value=True),
            _needs_post_processing=lambda *a, **k: False,
            _integrity_check_warning=AsyncMock(return_value=None),
        ):
            await self.manager._execute_download(download_id)
        await self.bus.drain_once()

        self.assertEqual(
            self.subscriber.names, [RECORDING_STARTED, RECORDING_COMPLETED]
        )
        payload = self.subscriber.of(RECORDING_COMPLETED)[0].recording
        self.assertEqual(payload["status"], DownloadStatus.INTERRUPTED.value)
        self.assertIn("stream ended", payload["interruption_reason"])


if __name__ == "__main__":
    unittest.main()
