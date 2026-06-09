"""
Regression tests: POST /vod/series/download must queue the episode batch
all-or-nothing, with a single batched duplicate check.

Before fix:
  - Episodes were built and queued one-by-one: build_episode_download issued
    its own lookups per episode and queue_download ran one duplicate-check
    SELECT plus one INSERT+COMMIT per episode (O(N) queries).
  - A failure mid-batch (e.g. episode 2 conflicting with an active download)
    raised after episode 1 had already been committed and enqueued, leaving a
    partial episode set with no rollback or report.

After fix:
  - All episodes are built first, duplicates are detected with one IN query
    for the whole batch, the rows are committed in a single transaction, and
    episodes are only enqueued after that commit. Any failure leaves nothing
    queued.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from models import AppSettings, Download, DownloadStatus, XtreamAccount
from services.download_manager import DownloadManager
from services.vod_namer import series_episode_output_path

DOWNLOAD_FOLDER = "/tmp/downloads"


def _episode_path(season: int, episode_num: int, title: str) -> str:
    return series_episode_output_path(
        DOWNLOAD_FOLDER, "Breaking Bad", season, episode_num, title, "mp4",
    )


def _make_active_download(output_path: str) -> Download:
    return Download(
        account_id=1,
        channel_id="s1",
        channel_name="On Demand Shows",
        program_title="Existing",
        program_start=datetime(2024, 1, 1),
        program_end=datetime(2024, 1, 1),
        duration_minutes=0,
        source_url="http://provider.test/old",
        output_path=output_path,
        status=DownloadStatus.DOWNLOADING.value,
        is_vod=True,
    )


class SeriesDownloadBatchTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_factory() as session:
            session.add(XtreamAccount(
                name="acc", server_url="http://provider.test",
                username="u", password="p",
            ))
            settings = AppSettings()
            settings.download_folder = DOWNLOAD_FOLDER
            session.add(settings)
            await session.commit()

        self.dm = DownloadManager()
        self.dm._broadcast_log = AsyncMock()

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _request(self, episodes):
        from api.vod import SeriesDownloadRequest, EpisodeItem
        return SeriesDownloadRequest(
            account_id=1,
            series_id="s1",
            series_name="Breaking Bad",
            episodes=[EpisodeItem(**ep) for ep in episodes],
        )

    def _auth(self):
        auth = MagicMock()
        auth.user_id = 1
        auth.provider = "admin_local"
        auth.is_admin = True
        return auth

    async def _call(self, data):
        from api.vod import download_series

        async with self.session_factory() as session:
            with (
                patch("api.vod.download_manager", self.dm),
                patch("api.vod.check_disk_space", new=AsyncMock()),
                patch(
                    "services.vod_service.resolve_account_password_with_migration",
                    new=AsyncMock(return_value="p"),
                ),
            ):
                return await download_series(data=data, auth=self._auth(), session=session)

    async def _fetch_new_downloads(self):
        async with self.session_factory() as session:
            result = await session.execute(
                select(Download).where(Download.program_title != "Existing")
            )
            return result.scalars().all()

    async def test_conflict_mid_batch_queues_nothing(self):
        """REGRESSION: a conflict on episode 2 must not leave episode 1 queued.

        Before fix episode 1 was committed and enqueued before episode 2's
        duplicate check raised, leaving a partial set behind a 409.
        """
        async with self.session_factory() as session:
            session.add(_make_active_download(_episode_path(1, 2, "Cat's in the Bag...")))
            await session.commit()

        data = self._request([
            {"id": "e1", "season": 1, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4"},
            {"id": "e2", "season": 1, "episode_num": 2, "title": "Cat's in the Bag...",
             "container_extension": "mp4"},
        ])

        with self.assertRaises(HTTPException) as ctx:
            await self._call(data)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("S01E02", ctx.exception.detail)
        self.assertEqual(
            await self._fetch_new_downloads(), [],
            "A mid-batch conflict must queue nothing (all-or-nothing).",
        )
        self.assertEqual(
            self.dm._queue.qsize(), 0,
            "Nothing may be put on the download queue when the batch is rejected.",
        )

    async def test_in_batch_duplicate_queues_nothing(self):
        """Two episodes resolving to the same output path reject the batch."""
        data = self._request([
            {"id": "e1", "season": 1, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4"},
            {"id": "e1b", "season": 1, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4"},
        ])

        with self.assertRaises(HTTPException) as ctx:
            await self._call(data)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(await self._fetch_new_downloads(), [])
        self.assertEqual(self.dm._queue.qsize(), 0)

    async def test_batch_success_uses_single_conflict_query(self):
        """A clean batch queues every episode and runs one conflict query total."""
        conflict_spy = AsyncMock(side_effect=self.dm.find_active_output_conflicts)

        data = self._request([
            {"id": "e1", "season": 1, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4"},
            {"id": "e2", "season": 1, "episode_num": 2, "title": "Cat's in the Bag...",
             "container_extension": "mp4"},
            {"id": "e3", "season": 1, "episode_num": 3, "title": "...And the Bag's in the River",
             "container_extension": "mp4"},
        ])

        with patch.object(self.dm, "find_active_output_conflicts", conflict_spy):
            result = await self._call(data)

        self.assertEqual(result["count"], 3)
        self.assertEqual(
            conflict_spy.await_count, 1,
            "Duplicate detection must run as one batched IN query, not per episode.",
        )
        paths = conflict_spy.await_args.args[1]
        self.assertEqual(len(paths), 3, "The single conflict query must cover all episodes.")

        rows = await self._fetch_new_downloads()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.status, DownloadStatus.PENDING.value)
        self.assertEqual(self.dm._queue.qsize(), 3)

    async def test_account_not_found_returns_404(self):
        """Unknown account still returns 404 before anything is queued."""
        data = self._request([
            {"id": "e1", "season": 1, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4"},
        ])
        data.account_id = 999

        with self.assertRaises(HTTPException) as ctx:
            await self._call(data)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(await self._fetch_new_downloads(), [])


if __name__ == "__main__":
    unittest.main()
