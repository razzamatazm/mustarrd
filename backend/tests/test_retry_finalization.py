"""Deferred raw attempts are finalized once, then safely cleaned by ownership."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from database import Base, _apply_lightweight_migrations
from models import AppSettings, Download, DownloadStatus, ScheduledRecording, ScheduledStatus, XtreamAccount
from services import retained_captures
from services.download_manager import DownloadManager
from services.retry_policy import HELD_CAPTURE_MAX_WAIT_HOURS
from services.scheduled_manager import ScheduledManager
from tests.test_short_download_retry import recording


class RetryFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw, self.completed = self.root / 'downloads', self.root / 'completed'
        self.raw.mkdir()
        self.completed.mkdir()
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add(XtreamAccount(id=1, name='Fixture', server_url='http://example.test', username='u', password='p'))
            session.add(AppSettings(download_folder=str(self.raw), completed_folder=str(self.completed),
                min_free_space_gb=0, mark_short_downloads_failed=True, auto_retry_failed_downloads=True,
                transcode_enabled=True, comskip_enabled=False, write_nfo_files=False,
                delete_original_after_transcode=True))
            self.download = recording(self.raw/'show.ts', duration_minutes=1)
            session.add(self.download)
            await session.commit()
        self.manager, self.scheduler = DownloadManager(), ScheduledManager()
        self.stack = ExitStack()
        self.stack.enter_context(patch('services.download_manager.async_session_maker', self.sessions))
        self.stack.enter_context(patch('services.scheduled_manager.async_session_maker', self.sessions))
        self.stack.enter_context(patch('services.scheduled_manager.download_manager', self.manager))
        self.free_space = self.stack.enter_context(patch.object(self.scheduler, '_get_free_space_gb', AsyncMock(return_value=1000)))
        self.archive = self.stack.enter_context(patch.object(self.scheduler, '_get_archive_days_raw', AsyncMock(return_value=7)))
        self.probe = self.stack.enter_context(patch('services.post_processor.post_processor._get_duration', AsyncMock(return_value=50)))
        self.stack.enter_context(patch.object(self.manager, '_partial_is_playable', AsyncMock(side_effect=lambda p: bool(p and Path(p).is_file() and Path(p).stat().st_size))))
        self.stack.enter_context(patch.object(self.manager, '_integrity_check_warning', AsyncMock(return_value=None)))
        self.stack.enter_context(patch.object(self.manager, '_trigger_plex_refresh', AsyncMock()))
        self.processed = []
        async def process(path, *args, **kwargs):
            self.processed.append(path)
            target = Path(path).with_suffix('.mkv')
            target.write_bytes(Path(path).read_bytes())
            return str(target), []
        self.processor = self.stack.enter_context(patch.object(self.manager, '_post_process', AsyncMock(side_effect=process)))

    async def asyncTearDown(self):
        self.stack.close()
        await self.engine.dispose()
        self.temp.cleanup()

    async def row(self):
        async with self.sessions() as session:
            return await session.get(Download, self.download.id)

    async def settings(self, **changes):
        async with self.sessions() as session:
            row = (await session.execute(select(AppSettings))).scalar_one()
            for key,value in changes.items():
                setattr(row,key,value)
            await session.commit()

    async def age(self, minutes=30):
        async with self.sessions() as session:
            row = await session.get(Download,self.download.id)
            row.last_retry_at = row.completed_at = datetime.utcnow()-timedelta(minutes=minutes)
            await session.commit()

    async def use_folders(self, downloads, completed, output):
        async with self.sessions() as session:
            settings = (await session.execute(select(AppSettings))).scalar_one()
            settings.download_folder, settings.completed_folder = str(downloads), str(completed)
            row = await session.get(Download, self.download.id)
            row.output_path = str(output)
            await session.commit()

    def symlinked_root(self):
        """The same folders reached through a symlink, as on a NAS share."""
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        return link

    async def stop_recovery_without_processing(self):
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        if not self.manager._post_queue.empty():
            await self.finish()

    async def stop_recovery(self):
        """Recovery ends without another capture: finalize what is held."""
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()

    async def capture(self, seconds=50, *, fail=False):
        row = await self.row()
        path = Path(row.output_path)
        self.probe.return_value = seconds
        async def transfer(*args, **kwargs):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(f'raw-attempt-{row.retry_count}-{seconds}'.encode())
            if fail:
                raise RuntimeError('Provider failed')
            return path.stat().st_size
        with patch.object(self.manager,'_download_catchup_stream',AsyncMock(side_effect=transfer)):
            if fail:
                with patch.object(self.manager,'_partial_is_playable',AsyncMock(return_value=False)):
                    await self.manager._execute_download(row.id)
            else:
                await self.manager._execute_download(row.id)
        return path

    async def retry(self):
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertEqual(self.manager._queue.get_nowait(),self.download.id)

    async def finish(self):
        self.assertEqual(self.manager._post_queue.get_nowait(),self.download.id)
        await self.manager._execute_post_process(self.download.id)

    async def test_four_short_captures_process_only_last_and_remove_superseded_files(self):
        paths=[]
        for attempt in range(4):
            paths.append(await self.capture())
            row=await self.row()
            if attempt < 3:
                self.assertTrue(row.processing_deferred)
                self.assertEqual(row.status,'failed')
                self.assertEqual(self.processed,[])
                self.assertTrue(self.manager._post_queue.empty())
                await self.retry()
        await self.finish()
        final=await self.row()
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.short_file_failed)
        self.assertTrue(final.retry_finalized)
        self.assertFalse(final.processing_deferred)
        self.assertEqual(final.retry_count,3)
        self.assertEqual(len(self.processed),1)
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue(self.manager._queue.empty())
        self.assertTrue(self.manager._post_queue.empty())

    async def test_complete_retry_processes_once_and_keeps_only_successful_output(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        await self.finish()
        final=await self.row()
        self.assertEqual(final.status,'completed')
        self.assertEqual(len(self.processed),1)
        self.assertFalse(old.exists())
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual(len(list(self.root.rglob('*.mkv'))),1)

    async def test_failed_last_transfer_finalizes_previous_playable_attempt(self):
        old=None
        for attempt in range(3):
            old=await self.capture()
            await self.retry()
        await self.capture(fail=True)
        final=await self.row()
        self.assertTrue(final.processing_deferred)
        self.assertEqual(final.retry_count,3)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(old)])
        self.assertTrue(Path(final.output_path).exists())
        self.assertIsNone(final.retained_captures_json)

    async def test_master_off_finalizes_without_recapture(self):
        await self.capture()
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        self.assertEqual((await self.row()).retry_count,0)
        self.assertTrue(self.manager._queue.empty())
        self.assertEqual(len(self.processed),1)

    async def test_classification_off_finalizes_as_interrupted(self):
        await self.capture()
        await self.settings(mark_short_downloads_failed=False)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        self.assertEqual((await self.row()).status,'interrupted')
        self.assertTrue(self.manager._queue.empty())

    async def test_short_classification_without_master_never_retries(self):
        await self.settings(auto_retry_failed_downloads=False)
        await self.capture()
        await self.finish()
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertEqual((await self.row()).status,'failed')
        self.assertTrue(self.manager._queue.empty())
        self.assertEqual(len(self.processed),1)

    async def test_archive_expired_finalizes_but_unreachable_provider_waits(self):
        await self.capture()
        await self.age()
        self.archive.side_effect=RuntimeError('unreachable')
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue((await self.row()).processing_deferred)
        self.assertTrue(self.manager._post_queue.empty())
        self.archive.side_effect=None
        self.archive.return_value=0
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        self.assertEqual((await self.row()).retry_count,0)

    async def test_processing_error_keeps_all_fallbacks(self):
        old=await self.capture()
        await self.retry()
        latest=await self.capture(60)
        self.processor.side_effect=RuntimeError('Comskip error')
        await self.finish()
        self.assertEqual((await self.row()).status,'failed')
        self.assertTrue(old.exists())
        self.assertTrue(latest.exists())
        self.assertIsNotNone((await self.row()).retained_captures_json)

    async def test_processing_warning_still_removes_superseded_attempts(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        self.processor.side_effect=None
        latest=(await self.row()).output_path
        self.processor.return_value=(latest,['Commercial removal skipped'])
        await self.finish()
        final=await self.row()
        self.assertIn('Commercial removal skipped',final.error_message)
        self.assertTrue(Path(final.output_path).exists())
        self.assertFalse(old.exists())
        self.assertIsNone(final.retained_captures_json)

    async def test_move_error_keeps_retained_files(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        with patch.object(self.manager,'_move_to_completed_async',AsyncMock(side_effect=OSError('full'))):
            await self.finish()
        self.assertTrue(old.exists())
        self.assertFalse((await self.row()).retry_finalized)

    async def test_final_validation_failure_keeps_fallbacks(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(return_value=False)):
            await self.finish()
        self.assertTrue(old.exists())
        self.assertIn('could not be verified',(await self.row()).error_message)

    async def test_changed_owned_file_is_not_deleted(self):
        old=await self.capture()
        await self.retry()
        old.write_bytes(b'replaced or modified by somebody else')
        await self.capture(60)
        await self.finish()
        self.assertEqual(old.read_bytes(),b'replaced or modified by somebody else')
        self.assertIn('changed',(await self.row()).error_message)

    async def test_unrelated_and_other_recording_files_are_protected(self):
        old=await self.capture()
        unrelated=self.raw/'show - retry 999.ts'
        unrelated.write_bytes(b'unrelated')
        async with self.sessions() as session:
            session.add(recording(old,program_title='Other owner'))
            await session.commit()
        await self.retry()
        await self.capture(60)
        await self.finish()
        self.assertTrue(old.exists())
        self.assertEqual(unrelated.read_bytes(),b'unrelated')

    async def test_keep_original_only_preserves_selected_not_all_retries(self):
        old=await self.capture()
        await self.retry()
        await self.settings(auto_retry_failed_downloads=False,delete_original_after_transcode=False)
        chosen=await self.capture()
        await self.finish()
        self.assertFalse(old.exists())
        self.assertTrue(chosen.exists())
        self.assertTrue(Path((await self.row()).output_path).exists())
        self.assertIsNone((await self.row()).retained_captures_json)

    async def test_keep_ts_finalizes_and_cleans_without_other_processing(self):
        old=await self.capture()
        await self.retry()
        await self.settings(transcode_enabled=False)
        await self.capture(60)
        final=await self.row()
        self.assertEqual(final.status,'completed')
        self.assertEqual(self.processed,[])
        self.assertTrue(Path(final.output_path).exists())
        self.assertFalse(old.exists())

    async def test_restart_resumes_post_commit_cleanup_without_processing_again(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        with patch.object(self.manager,'_cleanup_retained_attempts',AsyncMock()):
            await self.finish()
        self.assertTrue(old.exists())
        await self.manager.recover_retained_cleanup()
        self.assertFalse(old.exists())
        self.assertEqual(len(self.processed),1)
        self.assertIsNone((await self.row()).retained_captures_json)

    async def test_salvaged_short_output_stays_failed_and_does_not_claim_cleanup_safety(self):
        from services.recording_events import RECORDING_COMPLETED, RECORDING_FAILED
        await self.capture()
        row=await self.row()
        final=self.completed/'survived.ts'
        final.write_bytes(Path(row.output_path).read_bytes())
        with patch('services.download_manager.recording_events.publish') as publish:
            async with self.sessions() as session:
                await self.manager._finalize_completed_after_interrupt(
                    session,self.download.id,str(final),status='failed')
            self.assertEqual([call.args[0] for call in publish.call_args_list],
                [RECORDING_FAILED,RECORDING_COMPLETED])
        refreshed=await self.row()
        self.assertTrue(refreshed.short_file_failed)
        self.assertTrue(refreshed.retry_finalized)
        self.assertFalse(refreshed.processing_deferred)
        await self.manager.recover_retained_cleanup()
        self.assertTrue(Path(row.output_path).exists())
        self.assertTrue(final.exists())

    async def test_failed_final_file_can_be_served_but_deferred_file_cannot(self):
        from api.downloads import get_download_file
        from auth import AuthContext
        from fastapi import HTTPException
        from starlette.responses import FileResponse
        auth=AuthContext(authenticated=True,is_admin=True,user_id=1)
        await self.capture()
        async with self.sessions() as session:
            with self.assertRaises(HTTPException) as error:
                await get_download_file(self.download.id,None,"download",auth,session)
            self.assertEqual(error.exception.status_code,409)
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        async with self.sessions() as session:
            result=await get_download_file(self.download.id,None,"download",auth,session)
            self.assertIsInstance(result,FileResponse)

    async def test_clear_finished_preserves_deferred_ownership(self):
        from api.downloads import clear_finished_downloads
        from auth import AuthContext
        auth=AuthContext(authenticated=True,is_admin=True,user_id=1)
        old=await self.capture()
        async with self.sessions() as session:
            await clear_finished_downloads(auth,session)
        self.assertIsNotNone(await self.row())
        self.assertTrue(old.exists())

    async def test_cleanup_error_is_retryable_without_reprocessing(self):
        old=await self.capture()
        await self.retry()
        await self.capture(60)
        real_unlink=retained_captures.os.unlink
        def reject(path,*args,**kwargs):
            if Path(path).resolve()==old.resolve():
                raise PermissionError("busy")
            return real_unlink(path,*args,**kwargs)
        with patch('services.retained_captures.os.unlink',side_effect=reject):
            await self.finish()
        self.assertTrue(old.exists())
        self.assertIn('cleanup',(await self.row()).error_message)
        await self.manager.recover_retained_cleanup()
        self.assertFalse(old.exists())
        self.assertEqual(len(self.processed),1)

    async def test_expired_policy_recheck_cannot_queue_after_cancellation(self):
        await self.capture()
        await self.age()
        async def cancel_probe(path):
            self.manager._cancelled.add(self.download.id)
            return True
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(side_effect=cancel_probe)):
            self.assertFalse(await self.manager.finalize_deferred_capture(self.download.id,recovery_unavailable=True))
        self.assertTrue(self.manager._post_queue.empty())

    async def test_cancellation_stops_retries_and_finishes_the_held_capture(self):
        old=await self.capture()
        self.assertTrue(await self.manager.cancel_download(self.download.id))
        self.assertEqual((await self.row()).status,'cancelled')
        self.assertTrue(old.exists())
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue(self.manager._queue.empty())
        await self.finish()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.retry_count,0)
        self.assertEqual(final.output_path,str(self.completed/'show.mkv'))
        self.assertTrue(Path(final.output_path).exists())

    async def test_cancelling_a_retry_in_flight_finishes_the_held_capture(self):
        await self.capture(50)
        await self.retry()
        self.assertEqual((await self.row()).status,'pending')
        self.assertTrue(await self.manager.cancel_download(self.download.id))
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual(final.output_path,str(self.completed/'show.mkv'))
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_cancel_then_failed_manual_retry_still_finishes_the_held_capture(self):
        await self.capture(50)
        await self.retry()
        await self.manager.cancel_download(self.download.id)
        self.manager._cancelled.discard(self.download.id)
        self.assertTrue(await self.manager.retry_download(self.download.id))
        self.manager._queue.get_nowait()
        await self.capture(fail=True)
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertTrue(Path(final.output_path).exists())

    # --- the kept recording is the longest one, under its own name -------

    async def test_longest_attempt_wins_at_the_retry_cap(self):
        paths=[]
        for attempt, seconds in enumerate((50, 50, 50, 10)):
            paths.append(await self.capture(seconds))
            if attempt < 3:
                await self.retry()
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(paths[2])])
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual(final.status,'failed')
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_longest_attempt_wins_when_recovery_ends_early(self):
        longest=await self.capture(50)
        await self.retry()
        shorter=await self.capture(20)
        await self.stop_recovery()
        final=await self.row()
        self.assertEqual(self.processed,[str(longest)])
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertFalse(shorter.exists())
        self.assertIsNone(final.retained_captures_json)

    async def test_retry_names_do_not_pile_up_and_final_file_keeps_original_name(self):
        paths=[]
        for attempt in range(4):
            paths.append(await self.capture())
            if attempt < 3:
                await self.retry()
        self.assertEqual([p.name for p in paths],
            ['show.ts','show - retry 1.ts','show - retry 2.ts','show - retry 3.ts'])
        await self.finish()
        self.assertEqual((await self.row()).output_path,str(self.completed/'show.mkv'))

    async def test_complete_retry_lands_under_original_name(self):
        await self.capture()
        await self.retry()
        await self.capture(60)
        await self.finish()
        final=await self.row()
        self.assertEqual(final.status,'completed')
        self.assertEqual(final.output_path,str(self.completed/'show.mkv'))

    async def test_final_name_never_overwrites_an_existing_file(self):
        await self.capture()
        await self.retry()
        await self.capture(60)
        taken=self.completed/'show.mkv'
        taken.write_bytes(b'somebody else')
        await self.finish()
        final=await self.row()
        self.assertEqual(taken.read_bytes(),b'somebody else')
        self.assertEqual(final.output_path,str(self.completed/'show - retry 1.mkv'))

    async def test_held_capture_keeps_its_subfolder_through_a_symlinked_folder(self):
        link=self.symlinked_root()
        await self.use_folders(link/'downloads',link/'completed',link/'downloads'/'Show'/'Season 01'/'show.ts')
        await self.capture()
        await self.stop_recovery()
        final=await self.row()
        self.assertEqual(final.output_path,str(link/'completed'/'Show'/'Season 01'/'show.mkv'))
        self.assertTrue(Path(final.output_path).exists())

    async def test_only_copy_survives_when_one_symlinked_folder_holds_everything(self):
        link=self.symlinked_root()
        await self.use_folders(link/'downloads',link/'downloads',link/'downloads'/'show.ts')
        async def untouched(path,*args,**kwargs):
            self.processed.append(path)
            return path,[]
        self.processor.side_effect=untouched
        await self.capture()
        await self.stop_recovery()
        final=await self.row()
        self.assertEqual(final.output_path,str(link/'downloads'/'show.ts'))
        self.assertTrue(Path(final.output_path).exists())

    # --- a retry never replaces a recording with a worse one --------------

    async def test_option_switched_off_mid_retry_still_keeps_the_longest(self):
        longest=await self.capture(50)
        await self.retry()
        await self.settings(mark_short_downloads_failed=False)
        await self.capture(20)
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(longest)])
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')
        self.assertIsNone(final.retained_captures_json)

    async def test_retry_of_unknown_length_never_replaces_a_measured_capture(self):
        longest=await self.capture(50)
        await self.retry()
        await self.capture(None)
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(longest)])
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')

    async def kept_short_recording(self):
        await self.settings(auto_retry_failed_downloads=False)
        await self.capture(50)
        await self.finish()
        first=await self.row()
        self.assertEqual(Path(first.output_path).read_bytes(),b'raw-attempt-0-50')
        self.processed.clear()
        self.assertTrue(await self.manager.retry_download(self.download.id))
        self.manager._queue.get_nowait()
        return first

    async def test_manual_retry_that_comes_back_shorter_keeps_the_existing_recording(self):
        first=await self.kept_short_recording()
        await self.capture(20)
        final=await self.row()
        self.assertTrue(self.manager._post_queue.empty())
        self.assertEqual(self.processed,[])
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual(final.recorded_duration_seconds,first.recorded_duration_seconds)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_manual_retry_that_comes_back_longer_replaces_the_existing_recording(self):
        first=await self.kept_short_recording()
        await self.capture(55)
        await self.finish()
        final=await self.row()
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(final.raw_duration_seconds,55)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-55')
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_cancelling_a_manual_retry_restores_the_existing_recording(self):
        first=await self.kept_short_recording()
        self.assertTrue(await self.manager.cancel_download(self.download.id))
        await self.scheduler._auto_retry_failed_downloads()
        final=await self.row()
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertTrue(Path(final.output_path).exists())

    async def test_new_download_cannot_stream_over_a_held_capture(self):
        held=await self.capture()
        async with self.sessions() as session:
            with self.assertRaises(ValueError):
                await self.manager.queue_download(recording(held,program_title='Again'),session=session)
        self.assertEqual(held.read_bytes(),b'raw-attempt-0-50')

    async def test_short_failed_mark_survives_a_failed_final_commit(self):
        await self.settings(auto_retry_failed_downloads=False)
        await self.capture(50)
        names=await self.events()
        with patch.object(self.manager,'_transition_completed',AsyncMock(side_effect=RuntimeError('commit failed'))):
            await self.finish()
        row=await self.row()
        self.assertEqual(row.status,'failed')
        self.assertTrue(row.short_file_failed)
        self.assertIn('recording.failed',[name for name,_ in names])

    async def test_one_shared_folder_keeping_ts_gets_the_original_name_back(self):
        await self.settings(transcode_enabled=False)
        await self.use_folders(self.raw,self.raw,self.raw/'show.ts')
        await self.capture(20)
        await self.retry()
        await self.capture(50)
        await self.stop_recovery_without_processing()
        final=await self.row()
        self.assertEqual(final.output_path,str(self.raw/'show.ts'))
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-1-50')
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_option_off_interrupted_retry_still_replaces_the_kept_partial(self):
        await self.settings(mark_short_downloads_failed=False,auto_retry_failed_downloads=False,transcode_enabled=False)
        await self.use_folders(self.raw,self.raw,self.raw/'show.ts')
        row=await self.row()
        path=Path(row.output_path)
        async def transfer(*args,**kwargs):
            path.write_bytes(b'partial')
            raise RuntimeError('Provider dropped')
        with patch.object(self.manager,'_download_catchup_stream',AsyncMock(side_effect=transfer)):
            await self.manager._execute_download(row.id)
        self.assertEqual((await self.row()).status,'interrupted')
        self.assertTrue(await self.manager.retry_download(self.download.id))
        self.manager._queue.get_nowait()
        self.assertEqual(path.read_bytes(),b'partial')
        await self.capture(60)
        final=await self.row()
        self.assertEqual(final.status,'completed')
        self.assertEqual(final.output_path,str(path))
        self.assertEqual(path.read_bytes(),b'raw-attempt-0-60')
        self.assertIsNone(final.retained_captures_json)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[path])

    async def test_option_off_interrupted_retry_replaces_even_when_shorter(self):
        await self.settings(mark_short_downloads_failed=False,auto_retry_failed_downloads=False,transcode_enabled=False)
        row=await self.row()
        path=Path(row.output_path)
        self.probe.return_value=50
        async def transfer(*args,**kwargs):
            path.write_bytes(b'partial')
            raise RuntimeError('Provider dropped')
        with patch.object(self.manager,'_download_catchup_stream',AsyncMock(side_effect=transfer)):
            await self.manager._execute_download(row.id)
        kept=await self.row()
        self.assertEqual(kept.status,'interrupted')
        self.assertTrue(await self.manager.retry_download(self.download.id))
        self.manager._queue.get_nowait()
        await self.capture(20)
        final=await self.row()
        self.assertEqual(final.output_path,kept.output_path)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-20')

    # --- awkward moments: failed checks, cancels and processing errors ----

    async def test_kept_recording_that_cannot_be_checked_is_not_overwritten(self):
        first=await self.kept_short_recording()
        kept=Path(first.output_path)
        stat=kept.stat()
        os.utime(kept,ns=(stat.st_atime_ns,stat.st_mtime_ns+5_000_000_000))
        await self.capture(20)
        if not self.manager._post_queue.empty():
            await self.finish()
        final=await self.row()
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(kept.read_bytes(),b'raw-attempt-0-50')
        self.assertEqual(final.raw_duration_seconds,50)

    async def test_slow_probe_of_the_kept_recording_does_not_let_a_shorter_retry_win(self):
        first=await self.kept_short_recording()
        kept=first.output_path
        async def probe(path):
            return bool(path and os.path.realpath(path)!=os.path.realpath(kept) and Path(path).is_file())
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(side_effect=probe)):
            await self.capture(20)
        if not self.manager._post_queue.empty():
            await self.finish()
        final=await self.row()
        self.assertEqual(final.output_path,kept)
        self.assertEqual(Path(kept).read_bytes(),b'raw-attempt-0-50')

    async def test_implausible_measurement_never_beats_a_measured_capture(self):
        longest=await self.capture(50)
        await self.retry()
        await self.capture(5000)
        self.assertEqual((await self.row()).raw_duration_seconds,50)
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(longest)])
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')

    async def run_post_worker(self):
        task=asyncio.create_task(self.manager.process_post_queue())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if self.manager._post_queue.empty():
                break
        await asyncio.sleep(0.2)
        self.manager._running=False
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)

    async def test_cancelling_queued_processing_never_deletes_the_kept_attempt(self):
        longest=await self.capture(50)
        await self.retry()
        await self.capture(20)
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        self.assertEqual((await self.row()).output_path,str(longest))
        await self.manager.cancel_download(self.download.id)
        await self.run_post_worker()
        self.assertTrue(longest.exists())
        await self.scheduler._auto_retry_failed_downloads()
        row=await self.row()
        self.assertEqual(row.status,'cancelled')
        self.assertTrue(self.manager._post_queue.empty())
        self.assertEqual(self.processed,[])
        self.assertTrue(longest.exists())

    async def test_cancelling_processing_in_progress_sticks(self):
        held=await self.capture(50)
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        self.manager._post_queue.get_nowait()
        started=asyncio.Event()
        async def hang(path,*args,**kwargs):
            started.set()
            await asyncio.sleep(60)
        self.processor.side_effect=hang
        task=asyncio.create_task(self.manager._execute_post_process(self.download.id))
        self.manager._active_post[self.download.id]=task
        await started.wait()
        await self.manager.cancel_download(self.download.id)
        await asyncio.gather(task,return_exceptions=True)
        self.manager._active_post.pop(self.download.id,None)
        await self.scheduler._auto_retry_failed_downloads()
        row=await self.row()
        self.assertEqual(row.status,'cancelled')
        self.assertFalse(row.processing_deferred)
        self.assertTrue(self.manager._post_queue.empty())
        self.assertTrue(held.exists())

    async def test_cancel_after_the_last_byte_cannot_overwrite_the_kept_recording(self):
        await self.settings(transcode_enabled=False,auto_retry_failed_downloads=False)
        await self.capture(50)
        first=await self.row()
        self.assertTrue(await self.manager.retry_download(self.download.id))
        self.manager._queue.get_nowait()
        with patch.object(self.manager,'_prepare_downloaded_file',AsyncMock(side_effect=asyncio.CancelledError())):
            await self.capture(20)
        self.assertEqual(Path(first.output_path).read_bytes(),b'raw-attempt-0-50')
        await self.scheduler._auto_retry_failed_downloads()
        final=await self.row()
        self.assertEqual(final.output_path,first.output_path)
        self.assertTrue(final.retry_finalized)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_cancel_after_the_last_byte_still_keeps_the_longest_held_attempt(self):
        longest=await self.capture(50)
        await self.retry()
        with patch.object(self.manager,'_prepare_downloaded_file',AsyncMock(side_effect=asyncio.CancelledError())):
            await self.capture(20)
        self.assertTrue(longest.exists())
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(longest)])
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()],[Path(final.output_path)])

    async def test_better_retry_that_fails_processing_goes_back_to_the_kept_recording(self):
        first=await self.kept_short_recording()
        raw=await self.capture(55)
        self.processor.side_effect=RuntimeError('Comskip error')
        await self.finish()
        final=await self.row()
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(final.raw_duration_seconds,50)
        self.assertIn('Comskip error',final.error_message)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')
        self.assertTrue(raw.exists())
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue(self.manager._queue.empty())
        self.assertTrue(self.manager._post_queue.empty())

    async def test_one_failed_check_does_not_abandon_the_kept_recording(self):
        first=await self.kept_short_recording()
        await self.capture(fail=True)
        self.assertTrue((await self.row()).processing_deferred)
        await self.age()
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(return_value=False)):
            await self.scheduler._auto_retry_failed_downloads()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')

    async def test_unreadable_held_attempts_are_retried_then_given_up_after_the_bound(self):
        held=await self.capture(50)
        await self.settings(auto_retry_failed_downloads=False)
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(return_value=False)):
            await self.scheduler._auto_retry_failed_downloads()
            self.assertTrue((await self.row()).processing_deferred)
            await self.age(minutes=HELD_CAPTURE_MAX_WAIT_HOURS*60+1)
            await self.scheduler._auto_retry_failed_downloads()
        row=await self.row()
        self.assertFalse(row.processing_deferred)
        self.assertIn('could be verified',row.error_message)
        self.assertTrue(held.exists())

    async def held_retry_of_kept_recording(self, seconds):
        first=await self.kept_short_recording()
        await self.settings(auto_retry_failed_downloads=True)
        raw=await self.capture(seconds)
        self.assertTrue((await self.row()).processing_deferred)
        await self.settings(auto_retry_failed_downloads=False)
        return first,raw

    async def test_touched_kept_recording_does_not_stall_the_sweep(self):
        first=await self.kept_short_recording()
        await self.settings(auto_retry_failed_downloads=True)
        kept=Path(first.output_path)
        stat=kept.stat()
        os.utime(kept,ns=(stat.st_atime_ns,stat.st_mtime_ns+5_000_000_000))
        await self.capture(20)
        await self.settings(auto_retry_failed_downloads=False)
        await self.scheduler._auto_retry_failed_downloads()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertFalse(final.processing_deferred)
        self.assertEqual(final.output_path,first.output_path)

    async def test_one_failed_probe_of_a_longer_retry_does_not_settle_for_the_shorter_one(self):
        first,raw=await self.held_retry_of_kept_recording(53)
        calls=[]
        async def probe(path):
            calls.append(path)
            return len(calls)>1 and bool(path and Path(path).is_file())
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(side_effect=probe)):
            await self.scheduler._auto_retry_failed_downloads()
            self.assertTrue((await self.row()).processing_deferred)
            self.assertTrue(self.manager._post_queue.empty())
            await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertEqual(self.processed,[str(raw)])
        self.assertEqual(final.raw_duration_seconds,53)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-53')

    async def test_longer_retry_that_never_checks_out_settles_for_the_kept_recording_after_the_bound(self):
        first,raw=await self.held_retry_of_kept_recording(53)
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(return_value=False)):
            await self.scheduler._auto_retry_failed_downloads()
            self.assertTrue((await self.row()).processing_deferred)
            await self.age(minutes=HELD_CAPTURE_MAX_WAIT_HOURS*60+1)
            await self.scheduler._auto_retry_failed_downloads()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertTrue(raw.exists())

    async def test_kept_recording_with_no_recorded_length_is_not_replaced_by_a_short_retry(self):
        first=await self.kept_short_recording()
        async with self.sessions() as session:
            row=await session.get(Download,self.download.id)
            value=json.loads(row.retained_captures_json)
            for attempt in value['attempts']:
                attempt['raw_duration_seconds']=None
                attempt['duration_analysis_status']=None
            row.retained_captures_json=json.dumps(value)
            await session.commit()
        await self.capture(20)
        if not self.manager._post_queue.empty():
            await self.finish()
        final=await self.row()
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(Path(final.output_path).read_bytes(),b'raw-attempt-0-50')

    async def test_failed_fall_back_still_marks_the_recording_failed(self):
        await self.kept_short_recording()
        await self.capture(55)
        self.processor.side_effect=RuntimeError('ffmpeg error')
        with patch.object(self.manager,'_transition_completed',AsyncMock(side_effect=RuntimeError('database is locked'))):
            await self.finish()
        self.assertEqual((await self.row()).status,'failed')

    async def test_cancelling_the_processing_of_a_retry_goes_back_to_the_kept_recording(self):
        first=await self.kept_short_recording()
        raw=await self.capture(55)
        self.manager._post_queue.get_nowait()
        started=asyncio.Event()
        async def hang(path,*args,**kwargs):
            started.set()
            await asyncio.sleep(60)
        self.processor.side_effect=hang
        task=asyncio.create_task(self.manager._execute_post_process(self.download.id))
        self.manager._active_post[self.download.id]=task
        await started.wait()
        await self.manager.cancel_download(self.download.id)
        await asyncio.gather(task,return_exceptions=True)
        self.manager._active_post.pop(self.download.id,None)
        final=await self.row()
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertTrue(Path(first.output_path).exists())
        self.assertTrue(raw.exists())
        self.assertNotIn(self.download.id,self.manager._cancelled)

    async def test_cancelling_a_queued_retry_goes_back_to_the_kept_recording(self):
        first=await self.kept_short_recording()
        raw=await self.capture(55)
        self.assertEqual((await self.row()).status,'processing')
        await self.manager.cancel_download(self.download.id)
        await self.run_post_worker()
        final=await self.row()
        self.assertEqual(final.status,'failed')
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.output_path,first.output_path)
        self.assertEqual(self.processed,[])
        self.assertTrue(Path(first.output_path).exists())
        self.assertTrue(raw.exists())

    async def test_failure_after_switching_to_an_earlier_attempt_never_deletes_it(self):
        longest=await self.capture(50)
        await self.retry()
        await self.settings(auto_retry_failed_downloads=False)
        probes=[]
        async def probe(path):
            probes.append(path)
            return os.path.realpath(path)!=os.path.realpath(longest) or len(probes)<2
        with patch.object(self.manager,'_partial_is_playable',AsyncMock(side_effect=probe)), \
             patch.object(self.manager,'_transition_processing',AsyncMock(side_effect=RuntimeError('database is locked'))):
            await self.capture(20)
        self.assertTrue(longest.exists())
        self.assertEqual(longest.read_bytes(),b'raw-attempt-0-50')

    async def test_cancel_during_cleanup_leaves_the_kept_recording_in_its_subfolder(self):
        await self.use_folders(self.raw,self.completed,self.raw/'Show'/'S01'/'show.ts')
        first=await self.kept_short_recording()
        self.assertEqual(first.output_path,str(self.completed/'Show'/'S01'/'show.mkv'))
        with patch.object(self.manager,'_cleanup_retained_attempts',AsyncMock(side_effect=asyncio.CancelledError())):
            await self.capture(20)
        final=await self.row()
        self.assertEqual(final.output_path,first.output_path)
        self.assertTrue(Path(first.output_path).exists())

    # --- a held capture always has a way out ------------------------------

    async def test_unreachable_provider_finalizes_after_a_bounded_wait(self):
        await self.capture()
        self.archive.side_effect=RuntimeError('unreachable')
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue((await self.row()).processing_deferred)
        await self.age(minutes=HELD_CAPTURE_MAX_WAIT_HOURS*60+1)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        final=await self.row()
        self.assertFalse(final.processing_deferred)
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.retry_count,0)
        self.assertTrue(Path(final.output_path).exists())

    async def test_low_disk_space_finalizes_after_a_bounded_wait(self):
        await self.capture()
        self.free_space.return_value=None
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue((await self.row()).processing_deferred)
        await self.age(minutes=HELD_CAPTURE_MAX_WAIT_HOURS*60+1)
        await self.scheduler._auto_retry_failed_downloads()
        await self.finish()
        self.assertTrue((await self.row()).retry_finalized)

    async def test_cancelled_schedule_finalizes_the_held_capture_without_reviving_it(self):
        async with self.sessions() as session:
            schedule=ScheduledRecording(account_id=1,channel_id='12',channel_name='Example',
                program_title='Example program',program_start=self.download.program_start,
                program_end=self.download.program_end,duration_minutes=1,
                download_id=self.download.id,status=ScheduledStatus.CANCELLED.value)
            session.add(schedule)
            await session.commit()
        await self.capture()
        await self.age()
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue(self.manager._queue.empty())
        await self.finish()
        final=await self.row()
        self.assertTrue(final.retry_finalized)
        self.assertEqual(final.retry_count,0)
        self.assertTrue(Path(final.output_path).exists())
        async with self.sessions() as session:
            self.assertEqual((await session.get(ScheduledRecording,schedule.id)).status,'cancelled')

    async def test_held_recording_can_be_removed_from_history_and_keeps_its_file(self):
        from api.downloads import cancel_download
        from auth import AuthContext
        auth=AuthContext(authenticated=True,is_admin=True,user_id=1)
        held=await self.capture()
        with patch('api.downloads.download_manager',self.manager):
            async with self.sessions() as session:
                result=await cancel_download(self.download.id,auth,session)
        self.assertEqual(result,{'status':'deleted'})
        self.assertIsNone(await self.row())
        self.assertTrue(held.exists())
        await self.scheduler._auto_retry_failed_downloads()
        self.assertTrue(self.manager._queue.empty())
        self.assertTrue(self.manager._post_queue.empty())

    async def test_finished_recording_with_leftover_retry_files_can_be_cleared(self):
        from api.downloads import clear_finished_downloads
        from auth import AuthContext
        auth=AuthContext(authenticated=True,is_admin=True,user_id=1)
        old=await self.capture()
        await self.retry()
        old.write_bytes(b'replaced or modified by somebody else')
        await self.capture(60)
        await self.finish()
        final=await self.row()
        self.assertIsNotNone(final.retained_captures_json)
        async with self.sessions() as session:
            await clear_finished_downloads(auth,session)
        self.assertIsNone(await self.row())
        self.assertTrue(old.exists())
        self.assertTrue(Path(final.output_path).exists())

    async def test_cleanup_warning_is_not_repeated_on_every_restart(self):
        old=await self.capture()
        await self.retry()
        old.write_bytes(b'replaced or modified by somebody else')
        await self.capture(60)
        await self.finish()
        await self.manager.recover_retained_cleanup()
        await self.manager.recover_retained_cleanup()
        self.assertEqual((await self.row()).error_message.count('Retry-file cleanup'),1)

    # --- a file landed, so subscribers hear about it ----------------------

    async def events(self):
        names=[]
        publish=self.stack.enter_context(patch('services.download_manager.recording_events.publish_event'))
        publish.side_effect=lambda event: names.append((event.name,event.recording.get('status')))
        return names

    async def test_final_short_recording_announces_both_the_failure_and_the_file(self):
        names=await self.events()
        for attempt in range(4):
            await self.capture()
            if attempt < 3:
                await self.retry()
        await self.finish()
        self.assertEqual(names[-3:],[('postprocessing.completed','failed'),
            ('recording.failed','failed'),('recording.completed','failed')])

    async def test_marked_short_without_retries_still_announces_the_failure(self):
        names=await self.events()
        await self.settings(auto_retry_failed_downloads=False)
        await self.capture()
        await self.finish()
        self.assertEqual([name for name,_ in names if name!='recording.started'],
            ['postprocessing.completed','recording.failed','recording.completed'])

    async def test_failure_notification_survives_the_file_landing(self):
        from models import Notification
        from services.notifications import NotificationSubscriber
        from services.recording_events import RECORDING_COMPLETED, RECORDING_FAILED, recording_events
        notify=NotificationSubscriber(self.sessions)
        row=(await self.row()).to_dict()
        row.update(status='failed',short_file_failed=True,retry_finalized=True,error_message='Short')
        await notify(recording_events.build(RECORDING_FAILED,row))
        await notify(recording_events.build(RECORDING_COMPLETED,row))
        async with self.sessions() as session:
            self.assertEqual(len((await session.execute(select(Notification))).scalars().all()),1)
        row.update(status='completed',short_file_failed=False)
        await notify(recording_events.build(RECORDING_COMPLETED,row))
        async with self.sessions() as session:
            self.assertEqual((await session.execute(select(Notification))).scalars().all(),[])

    async def test_final_short_recording_is_eligible_for_a_poster(self):
        from services.poster_art import is_poster_eligible
        await self.capture()
        self.assertFalse(is_poster_eligible(await self.row()))
        await self.stop_recovery()
        self.assertTrue(is_poster_eligible(await self.row()))

    # --- with the option off, nothing changes from before -----------------

    async def test_option_off_short_capture_still_completes(self):
        await self.settings(mark_short_downloads_failed=False,auto_retry_failed_downloads=False)
        await self.capture()
        await self.finish()
        final=await self.row()
        self.assertEqual(final.status,'completed')
        self.assertIsNone(final.interruption_reason)
        self.assertEqual(final.duration_analysis_status,'short')
        self.assertFalse(final.short_file_failed)
        self.assertIsNone(final.retained_captures_json)

    async def test_option_off_failed_processing_retries_in_place(self):
        await self.settings(mark_short_downloads_failed=False)
        raw=await self.capture()
        self.processor.side_effect=RuntimeError('Comskip error')
        await self.finish()
        self.assertEqual((await self.row()).status,'failed')
        await self.retry()
        self.assertEqual((await self.row()).output_path,str(raw))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_real_media_four_short_attempts_keep_one_final_file(self):
        fixture=self.root/'fixture.ts'
        subprocess.run([shutil.which('ffmpeg'),'-hide_banner','-loglevel','error','-f','lavfi',
            '-i','color=c=black:s=32x32:r=1','-t','50','-c:v','mpeg2video','-an','-f','mpegts',str(fixture)],
            check=True,timeout=15,capture_output=True)
        media=fixture.read_bytes()
        fixture.unlink()
        from services.post_processor import post_processor
        # Use actual probing and actual postprocessing, isolating network only.
        self.stack.close()
        with ExitStack() as stack:
            stack.enter_context(patch('services.download_manager.async_session_maker',self.sessions))
            stack.enter_context(patch('services.scheduled_manager.async_session_maker',self.sessions))
            stack.enter_context(patch('services.scheduled_manager.download_manager',self.manager))
            stack.enter_context(patch.object(self.scheduler,'_get_free_space_gb',AsyncMock(return_value=1000)))
            stack.enter_context(patch.object(self.scheduler,'_get_archive_days_raw',AsyncMock(return_value=7)))
            stack.enter_context(patch.object(self.manager,'_trigger_plex_refresh',AsyncMock()))
            await self.settings(transcode_enabled=False)
            for attempt in range(4):
                row=await self.row()
                async def transfer(*args,**kwargs):
                    Path(row.output_path).write_bytes(media)
                    return len(media)
                with patch.object(self.manager,'_download_catchup_stream',AsyncMock(side_effect=transfer)):
                    await self.manager._execute_download(row.id)
                if attempt<3:
                    self.assertTrue((await self.row()).processing_deferred)
                    await self.retry()
            final=await self.row()
            self.assertEqual(final.status,'failed')
            self.assertTrue(final.retry_finalized)
            self.assertEqual(final.retry_count,3)
            self.assertAlmostEqual(final.raw_duration_seconds,50,delta=1)
            self.assertEqual(Path(final.output_path).read_bytes(),media)
            self.assertEqual(len([p for p in self.root.rglob('*') if p.is_file()]),1)


class RetainedIdentityTests(unittest.TestCase):
    def test_symlink_replacement_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'owned.ts'
            target=Path(folder)/'other.ts'
            path.write_bytes(b'owned')
            receipt=retained_captures.identity(str(path))
            path.unlink()
            target.write_bytes(b'other')
            path.symlink_to(target)
            remaining,warnings=retained_captures.cleanup({'attempts':[{'media':receipt}]},[],False)
            self.assertTrue(path.is_symlink())
            self.assertTrue(target.exists())
            self.assertTrue(warnings)
            self.assertTrue(remaining['attempts'])

    def test_only_registered_companions_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            media=Path(folder)/'old.ts'
            companion=media.with_suffix('.edl')
            unrelated=media.with_suffix('.log')
            for p in (media,companion,unrelated): p.write_bytes(b'data')
            value={'attempts':[{'media':retained_captures.identity(str(media)),
                'companions':[retained_captures.identity(str(companion))]}]}
            remaining,errors=retained_captures.cleanup(value,[],False)
            self.assertFalse(media.exists())
            self.assertFalse(companion.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(remaining['attempts'],[])
            self.assertEqual(errors,[])


class RetrySettingMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_switch_migrates_once_without_enabling_master(self):
        engine=create_async_engine('sqlite+aiosqlite:///:memory:')
        try:
            async with engine.begin() as conn:
                for table in ('plex_servers','app_settings','epg_programs','xtream_accounts','scheduled_recordings','downloads'):
                    await conn.execute(text(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)'))
                await conn.execute(text('ALTER TABLE app_settings ADD COLUMN auto_retry_short_downloads BOOLEAN DEFAULT 0'))
                await conn.execute(text('INSERT INTO app_settings(id,auto_retry_short_downloads) VALUES (1,1)'))
                await _apply_lightweight_migrations(conn)
                vals=(await conn.execute(text('SELECT mark_short_downloads_failed,auto_retry_failed_downloads FROM app_settings'))).one()
                self.assertEqual(tuple(vals),(1,0))
                await conn.execute(text('UPDATE app_settings SET mark_short_downloads_failed=0'))
                await _apply_lightweight_migrations(conn)
                self.assertEqual((await conn.execute(text('SELECT mark_short_downloads_failed FROM app_settings'))).scalar_one(),0)
        finally:
            await engine.dispose()
