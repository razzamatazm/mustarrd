import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from config import settings as app_config
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    EPGProgram,
    RecordingRule,
    ScheduledRecording,
    XtreamAccount,
)


logger = logging.getLogger(__name__)


class ExactNormalizedTitleMatcher:
    """Case-insensitive exact match after trimming/collapsing whitespace."""

    @staticmethod
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).casefold()

    def matches(self, title: str, title_match: str) -> bool:
        return self.normalize(title) == self.normalize(title_match)


exact_title_matcher = ExactNormalizedTitleMatcher()


class ContainsNormalizedTitleMatcher:
    """Case-insensitive substring match after normalizing whitespace."""

    def matches(self, title: str, title_match: str) -> bool:
        return exact_title_matcher.normalize(title_match) in exact_title_matcher.normalize(title)


class RegexTitleMatcher:
    """Case-insensitive regular-expression search against the programme title."""

    def matches(self, title: str, title_match: str) -> bool:
        try:
            return re.search(title_match, title or "", re.IGNORECASE) is not None
        except re.error:
            logger.warning("Skipping recording rule with invalid regex: %r", title_match)
            return False


title_matchers = {
    "exact": exact_title_matcher,
    "contains": ContainsNormalizedTitleMatcher(),
    "regex": RegexTitleMatcher(),
}


def get_title_matcher(match_mode: str | None):
    return title_matchers.get(match_mode or "exact", exact_title_matcher)


def build_rule_airing_key(program: EPGProgram) -> str:
    """Stable identity for one provider/channel airing."""
    return (
        f"{program.account_id}:{program.channel_id}:"
        f"{program.start_timestamp}:{program.stop_timestamp}"
    )


class RecordingRuleService:
    @staticmethod
    def _allowed_recording_roots(db_settings: AppSettings | None) -> list[Path]:
        roots = [
            app_config.default_download_folder,
            app_config.default_completed_folder,
        ]
        if db_settings:
            roots.extend(
                path
                for path in (db_settings.download_folder, db_settings.completed_folder)
                if path
            )
        return list(
            dict.fromkeys(Path(path).expanduser().resolve() for path in roots)
        )

    async def delete_expired_downloads(
        self,
        session: AsyncSession,
        *,
        account_id: int | None = None,
        rule_id: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """Delete opted-in, expired media owned by recurring recording rules.

        A file is only eligible when the completed Download is linked through a
        schedule created by the rule and its resolved path is below a configured
        recording root. Missing or unsafe paths are left untouched for review.
        """
        query = (
            select(RecordingRule, ScheduledRecording, Download)
            .join(
                ScheduledRecording,
                ScheduledRecording.recording_rule_id == RecordingRule.id,
            )
            .join(Download, Download.id == ScheduledRecording.download_id)
            .where(
                RecordingRule.delete_after_days.is_not(None),
                Download.status == DownloadStatus.COMPLETED.value,
                Download.completed_at.is_not(None),
            )
        )
        if account_id is not None:
            query = query.where(RecordingRule.account_id == account_id)
        if rule_id is not None:
            query = query.where(RecordingRule.id == rule_id)

        rows = (await session.execute(query)).all()
        if not rows:
            return 0

        db_settings = (
            await session.execute(select(AppSettings))
        ).scalar_one_or_none()
        allowed_roots = self._allowed_recording_roots(db_settings)
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        deleted = 0
        for rule, schedule, download in rows:
            completed_at = download.completed_at
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            if completed_at > now_utc - timedelta(days=rule.delete_after_days):
                continue
            if not download.output_path:
                logger.warning(
                    "Retention skipped download %s because it has no output path",
                    download.id,
                )
                continue

            file_path = Path(download.output_path).expanduser().resolve()
            if not any(file_path.is_relative_to(root) for root in allowed_roots):
                logger.warning(
                    "Retention refused to delete download %s outside configured roots: %s",
                    download.id,
                    file_path,
                )
                continue
            if not file_path.is_file():
                logger.warning(
                    "Retention skipped download %s because its file is missing: %s",
                    download.id,
                    file_path,
                )
                continue

            try:
                await asyncio.to_thread(file_path.unlink)
            except OSError:
                logger.exception(
                    "Retention could not delete download %s at %s",
                    download.id,
                    file_path,
                )
                continue

            schedule.download_id = None
            schedule.status_message = (
                f"Recording deleted after {rule.delete_after_days} days by recurring rule"
            )
            await session.delete(download)
            deleted += 1

        if deleted:
            await session.commit()
        return deleted

    async def evaluate(
        self,
        session: AsyncSession,
        *,
        account_id: int | None = None,
        rule_id: int | None = None,
    ) -> int:
        """Create ordinary schedules for future EPG rows matching enabled rules."""
        rule_query = select(RecordingRule).where(RecordingRule.enabled.is_(True))
        if account_id is not None:
            rule_query = rule_query.where(RecordingRule.account_id == account_id)
        if rule_id is not None:
            rule_query = rule_query.where(RecordingRule.id == rule_id)

        rules = (await session.execute(rule_query)).scalars().all()
        if not rules:
            return 0

        account_ids = {rule.account_id for rule in rules}
        channel_ids = {rule.channel_id for rule in rules}
        now_timestamp = int(datetime.now(timezone.utc).timestamp())
        program_query = (
            select(EPGProgram)
            .where(
                EPGProgram.account_id.in_(account_ids),
                EPGProgram.channel_id.in_(channel_ids),
                EPGProgram.start_timestamp > now_timestamp,
            )
            .order_by(EPGProgram.start_timestamp.asc())
        )
        programs = (await session.execute(program_query)).scalars().all()
        account_offsets = dict(
            (
                await session.execute(
                    select(XtreamAccount.id, XtreamAccount.guide_offset_hours).where(
                        XtreamAccount.id.in_(account_ids)
                    )
                )
            ).all()
        )
        airing_keys = {build_rule_airing_key(program) for program in programs}
        existing_airing_keys = set()
        if airing_keys:
            existing_airing_keys = set(
                (
                    await session.execute(
                        select(ScheduledRecording.rule_airing_key).where(
                            ScheduledRecording.rule_airing_key.in_(airing_keys)
                        )
                    )
                ).scalars().all()
            )

        rules_by_channel: dict[tuple[int, str], list[RecordingRule]] = defaultdict(list)
        for rule in rules:
            rules_by_channel[(rule.account_id, rule.channel_id)].append(rule)

        # Imported lazily to avoid coupling API module import order to service setup.
        # This is the same creation function used by POST /schedules and time slots.
        from api.schedules import ScheduleCreate, _create_schedule_record

        created = 0
        for program in programs:
            airing_key = build_rule_airing_key(program)
            if airing_key in existing_airing_keys:
                continue
            channel_key = (program.account_id, program.channel_id)
            for rule in rules_by_channel.get(channel_key, []):
                matcher = get_title_matcher(rule.match_mode)
                if not matcher.matches(program.title, rule.title_match):
                    continue
                days_of_week = rule.parsed_days_of_week
                if days_of_week is not None:
                    air_day = datetime.fromtimestamp(
                        program.start_timestamp, tz=timezone.utc
                    ) + timedelta(hours=int(account_offsets.get(program.account_id) or 0))
                    air_day = air_day.weekday()
                    if air_day not in days_of_week:
                        continue

                auth = AuthContext(
                    authenticated=True,
                    role="download_only",
                    provider=rule.request_source,
                    user_id=rule.requested_by_user_id,
                    is_admin=rule.requested_by_user_id is None,
                )
                payload = ScheduleCreate(
                    account_id=program.account_id,
                    channel_id=program.channel_id,
                    channel_name=program.channel_name,
                    program={
                        "id": program.id,
                        "epg_id": program.epg_id,
                        "title": program.title,
                        "description": program.description,
                        "category": program.category,
                        "start_time": program.start_time.isoformat(),
                        "end_time": program.end_time.isoformat(),
                        "start_timestamp": program.start_timestamp,
                        "stop_timestamp": program.stop_timestamp,
                        "provider_start": program.provider_start,
                        "provider_stop": program.provider_stop,
                    },
                    pre_padding_minutes=rule.pre_padding_minutes,
                    post_padding_minutes=rule.post_padding_minutes,
                )
                try:
                    await _create_schedule_record(
                        payload,
                        auth,
                        session,
                        recording_rule_id=rule.id,
                        rule_airing_key=airing_key,
                    )
                    created += 1
                    existing_airing_keys.add(airing_key)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    # A manual schedule or another rule already owns this airing.
                except IntegrityError:
                    # The unique airing key closes the race between concurrent
                    # rule evaluations and makes refresh processing idempotent.
                    await session.rollback()
                    return created

        # keep_count remains reserved for count-based retention. Age-based
        # deletion is implemented separately by delete_expired_downloads().
        return created


recording_rule_service = RecordingRuleService()
