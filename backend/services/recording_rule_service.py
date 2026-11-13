import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import AuthContext
from models import (
    AppSettings,
    Download,
    DownloadStatus,
    EPGProgram,
    RecordingRule,
    ScheduledRecording,
    ScheduledStatus,
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


def build_rule_airing_key(program) -> str:
    """Stable identity for one provider/channel listing.

    Keyed on the exact provider start/stop, which survives EPG re-ingest (the
    important case). A listing the provider moves gets a different key; that
    case is caught by ``_Covered.is_same_showing`` rather than by the key.
    """
    return (
        f"{program.account_id}:{program.channel_id}:"
        f"{program.start_timestamp}:{program.stop_timestamp}"
    )


# A rule's recording has "fired" once the scheduler has handed it to the
# download queue. Before that it is only an intention, and it is the rule's.
UNFIRED_SCHEDULE_STATUSES = (
    ScheduledStatus.SCHEDULED.value,
    ScheduledStatus.PAUSED_LOW_SPACE.value,
)

# A schedule in one of these states does not mean the airing was, or will be,
# recorded, so it does not stop a rule from scheduling that airing.
_NOT_TAKEN_SCHEDULE_STATUSES = (
    ScheduledStatus.CANCELLED.value,
    ScheduledStatus.FAILED.value,
)
_TAKEN_DOWNLOAD_STATUSES = (
    DownloadStatus.PENDING.value,
    DownloadStatus.DOWNLOADING.value,
    DownloadStatus.PROCESSING.value,
    DownloadStatus.COMPLETED.value,
    DownloadStatus.INTERRUPTED.value,
)

# Saving a rule also catches what is on air now and what finished recently,
# as long as catchup still has it. "Recently" is bounded: without a bound, a
# rule for an hourly title on an account with a week of catchup queued more
# than 160 recordings at once, all due immediately.
BACKFILL_HOURS = 24
# How many not-yet-started recordings one rule may have waiting. A loose
# pattern ("contains a") matches every programme on a channel; this turns
# that into a rolling window, nearest first, topped up after each refresh.
MAX_PENDING_SCHEDULES_PER_RULE = 100
# Longest title text a pattern is ever run against.
MAX_MATCHED_TITLE_LENGTH = 300
MAX_TITLE_MATCH_LENGTH = 200


@dataclass(frozen=True)
class _Airing:
    """One guide programme, copied out of the ORM.

    Evaluation commits (and on a lost race, rolls back) as it goes, and either
    one expires ORM rows. Plain copies keep the loop from touching them.
    """

    id: int
    account_id: int
    channel_id: str
    channel_name: str
    epg_id: str | None
    title: str
    description: str | None
    category: str | None
    start_time: datetime
    end_time: datetime
    start_timestamp: int
    stop_timestamp: int
    provider_start: str | None
    provider_stop: str | None

    @property
    def slot(self) -> tuple[int, str, int, int]:
        return (self.account_id, self.channel_id, self.start_timestamp, self.stop_timestamp)

    @property
    def key(self) -> str:
        return build_rule_airing_key(self)


@dataclass
class _Covered:
    """A stretch of a channel that is already scheduled, recording or recorded.

    Providers retime listings between guide refreshes: a match overruns, a
    slot is corrected from 8:00 to 8:05. The guide keeps the old listing and
    gains the new one beside it, so one showing appears twice with slightly
    different times. Exact start/stop cannot tell that these are the same
    showing; title plus overlap can.
    """

    account_id: int
    channel_id: str
    title: str
    start_timestamp: int
    stop_timestamp: int
    # Set only for a rule's recording that has not started: the one kind that
    # can still be moved to follow its listing.
    schedule_id: int | None = None
    # The guide row that recording was made from.
    listing_id: int | None = None

    def overlap_with(self, airing: _Airing) -> int:
        return min(self.stop_timestamp, airing.stop_timestamp) - max(
            self.start_timestamp, airing.start_timestamp
        )

    def is_same_showing(self, airing: _Airing) -> bool:
        """Same title, and the two overlap for most of the shorter one.

        "Most of" is what keeps back-to-back episodes of one show apart: after
        a five minute move, an episode overlaps its own old listing almost
        entirely and its neighbour's by five minutes.
        """
        if self.title != exact_title_matcher.normalize(airing.title):
            return False
        shorter = min(
            self.stop_timestamp - self.start_timestamp,
            airing.stop_timestamp - airing.start_timestamp,
        )
        return shorter > 0 and self.overlap_with(airing) * 2 > shorter

    def can_follow(self, airing: _Airing) -> bool:
        """Whether this is a waiting recording and *airing* is its newer listing.

        Guide rows are only ever added, so the higher row id is the listing
        the provider sent more recently. Following only newer rows also keeps
        the old listing, still in the guide, from pulling the recording back.
        """
        return (
            self.schedule_id is not None
            and self.listing_id is not None
            and airing.id > self.listing_id
        )


def _listing_id(program_id: str | None) -> int | None:
    try:
        return int(program_id) if program_id is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class _Rule:
    id: int
    account_id: int
    channel_id: str
    title_match: str
    match_mode: str
    days_of_week: tuple[int, ...] | None
    pre_padding_minutes: int
    post_padding_minutes: int
    request_source: str | None
    requested_by_user_id: int | None


class RecordingRuleService:
    def __init__(self) -> None:
        # One evaluation or deletion at a time. Guide refreshes for two
        # accounts, a rule being saved, and a rule being deleted would
        # otherwise interleave: the same airing scheduled twice, or schedules
        # created for a rule that has just been deleted.
        self.lock = asyncio.Lock()

    async def remove_unfired_schedules(self, session: AsyncSession, rule_id: int) -> int:
        """Delete the recordings a rule scheduled that have not started yet.

        Called when the rule is deleted (#427: a deleted rule must not leave
        orphaned scheduled recordings behind). Anything already downloading,
        finished, failed or cancelled is history and is left alone. The rows
        are deleted rather than cancelled: a cancelled row would keep its
        airing key and stop a later rule from ever scheduling that airing.
        Does not commit; the caller commits with the rule's own deletion.
        """
        result = await session.execute(
            delete(ScheduledRecording).where(
                ScheduledRecording.recording_rule_id == rule_id,
                ScheduledRecording.status.in_(UNFIRED_SCHEDULE_STATUSES),
                ScheduledRecording.download_id.is_(None),
            )
        )
        return int(result.rowcount or 0)

    async def delete_rules(
        self,
        session: AsyncSession,
        *,
        rule_id: int | None = None,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> int:
        """Delete rules and tidy up after them. Returns unfired schedules removed.

        Exactly one selector: a single rule, everything a user owns (the user
        is being deleted), or everything on an account (the account is being
        deleted). A rule left behind in either case would keep scheduling
        recordings for someone or something that no longer exists. Does not
        commit. Callers hold ``self.lock``.
        """
        query = select(RecordingRule.id)
        if rule_id is not None:
            query = query.where(RecordingRule.id == rule_id)
        elif user_id is not None:
            query = query.where(RecordingRule.requested_by_user_id == user_id)
        elif account_id is not None:
            query = query.where(RecordingRule.account_id == account_id)
        else:
            raise ValueError("delete_rules needs a rule, a user or an account")

        removed = 0
        for found_id in (await session.execute(query)).scalars().all():
            removed += await self.remove_unfired_schedules(session, found_id)
            # Everything that has started, finished or failed is history: it
            # is detached and kept.
            await session.execute(
                update(ScheduledRecording)
                .where(ScheduledRecording.recording_rule_id == found_id)
                .values(recording_rule_id=None)
            )
            await session.execute(delete(RecordingRule).where(RecordingRule.id == found_id))
        return removed

    async def evaluate(
        self,
        session: AsyncSession,
        *,
        account_id: int | None = None,
        rule_id: int | None = None,
        backfill: bool = False,
    ) -> int:
        """Create ordinary schedules for guide programmes matching enabled rules.

        After a guide refresh (``backfill=False``) only programmes that have
        not finished are considered. When a rule is created or edited
        (``backfill=True``) programmes that finished in the last
        ``BACKFILL_HOURS`` are included too, if catchup still has them, so a
        rule made while tonight's episode is on air still catches it.

        An airing that is already scheduled, recording or recorded, by hand
        or by another rule, is left alone. So is a listing the provider has
        only moved: it is the same showing, and if the rule's recording for
        it has not started, that recording is moved to the new time instead.
        """
        async with self.lock:
            return await self._evaluate(
                session, account_id=account_id, rule_id=rule_id, backfill=backfill
            )

    async def _evaluate(
        self,
        session: AsyncSession,
        *,
        account_id: int | None,
        rule_id: int | None,
        backfill: bool,
    ) -> int:
        rule_query = select(RecordingRule).where(RecordingRule.enabled.is_(True))
        if account_id is not None:
            rule_query = rule_query.where(RecordingRule.account_id == account_id)
        if rule_id is not None:
            rule_query = rule_query.where(RecordingRule.id == rule_id)

        rules = [
            _Rule(
                id=rule.id,
                account_id=rule.account_id,
                channel_id=rule.channel_id,
                title_match=rule.title_match,
                match_mode=rule.match_mode or "exact",
                days_of_week=(
                    tuple(rule.parsed_days_of_week)
                    if rule.parsed_days_of_week is not None
                    else None
                ),
                pre_padding_minutes=rule.pre_padding_minutes,
                post_padding_minutes=rule.post_padding_minutes,
                request_source=rule.request_source,
                requested_by_user_id=rule.requested_by_user_id,
            )
            for rule in (await session.execute(rule_query)).scalars().all()
        ]
        if not rules:
            return 0

        account_ids = {rule.account_id for rule in rules}
        channel_ids = {rule.channel_id for rule in rules}
        now_timestamp = int(datetime.now(timezone.utc).timestamp())

        accounts = (
            await session.execute(
                select(XtreamAccount).where(XtreamAccount.id.in_(account_ids))
            )
        ).scalars().all()
        account_offsets = {
            account.id: int(account.guide_offset_hours or 0) for account in accounts
        }
        # How far back each account's catchup goes. Prefer the provider-reported
        # window, fall back to the configured catchup_days.
        account_archive_seconds = {
            account.id: max(
                int(account.catchup_max_archive_days or account.catchup_days or 0), 0
            )
            * 86400
            for account in accounts
        }
        db_settings = (
            await session.execute(select(AppSettings))
        ).scalar_one_or_none()
        global_offset_minutes = int(
            getattr(db_settings, "epg_offset_minutes", 0) or 0
        )

        backfill_seconds = BACKFILL_HOURS * 3600 if backfill else 0

        def earliest_stop_for(found_account_id: int) -> int:
            reach = min(account_archive_seconds.get(found_account_id, 0), backfill_seconds)
            return now_timestamp - reach

        earliest_stop = min(earliest_stop_for(found) for found in account_ids)
        program_rows = (
            await session.execute(
                select(EPGProgram)
                .where(
                    EPGProgram.account_id.in_(account_ids),
                    EPGProgram.channel_id.in_(channel_ids),
                    EPGProgram.stop_timestamp > earliest_stop,
                )
                .order_by(EPGProgram.start_timestamp.asc())
            )
        ).scalars().all()
        airings = [
            _Airing(
                id=program.id,
                account_id=program.account_id,
                channel_id=program.channel_id,
                channel_name=program.channel_name,
                epg_id=program.epg_id,
                title=program.title or "",
                description=program.description,
                category=program.category,
                start_time=program.start_time,
                end_time=program.end_time,
                start_timestamp=program.start_timestamp,
                stop_timestamp=program.stop_timestamp,
                provider_start=program.provider_start,
                provider_stop=program.provider_stop,
            )
            for program in program_rows
        ]
        if not airings:
            return 0

        taken_keys = set(
            (
                await session.execute(
                    select(ScheduledRecording.rule_airing_key).where(
                        ScheduledRecording.rule_airing_key.in_({airing.key for airing in airings})
                    )
                )
            ).scalars().all()
        )
        # Already scheduled by hand, recording, or recorded: by anyone, by any
        # route. A rule made after you recorded an episode yourself must not
        # record it a second time. A recording the rule itself made counts
        # whatever became of it, exactly as its airing key does: an episode
        # you cancelled stays cancelled when its listing moves.
        #
        # Reaches back to the earliest listing under consideration rather than
        # to "now": a recording whose listing has just ended still covers the
        # moved listing that is still on air.
        covered_since = min(airing.start_timestamp for airing in airings)
        covered: dict[tuple[int, str], list[_Covered]] = defaultdict(list)
        for row in (
            await session.execute(
                select(
                    ScheduledRecording.id,
                    ScheduledRecording.account_id,
                    ScheduledRecording.channel_id,
                    ScheduledRecording.program_title,
                    ScheduledRecording.start_timestamp,
                    ScheduledRecording.stop_timestamp,
                    ScheduledRecording.status,
                    ScheduledRecording.download_id,
                    ScheduledRecording.program_id,
                    ScheduledRecording.recording_rule_id,
                ).where(
                    ScheduledRecording.account_id.in_(account_ids),
                    ScheduledRecording.channel_id.in_(channel_ids),
                    ScheduledRecording.stop_timestamp > covered_since,
                    or_(
                        ScheduledRecording.status.not_in(_NOT_TAKEN_SCHEDULE_STATUSES),
                        ScheduledRecording.rule_airing_key.is_not(None),
                    ),
                )
            )
        ).all():
            waiting_for_a_rule = (
                row.recording_rule_id is not None
                and row.status in UNFIRED_SCHEDULE_STATUSES
                and row.download_id is None
            )
            covered[(row.account_id, row.channel_id)].append(
                _Covered(
                    account_id=row.account_id,
                    channel_id=row.channel_id,
                    title=exact_title_matcher.normalize(row.program_title),
                    start_timestamp=row.start_timestamp,
                    stop_timestamp=row.stop_timestamp,
                    schedule_id=row.id if waiting_for_a_rule else None,
                    listing_id=_listing_id(row.program_id) if waiting_for_a_rule else None,
                )
            )
        for row in (
            await session.execute(
                select(
                    Download.account_id,
                    Download.channel_id,
                    Download.program_title,
                    Download.start_timestamp,
                    Download.stop_timestamp,
                ).where(
                    Download.account_id.in_(account_ids),
                    Download.channel_id.in_(channel_ids),
                    Download.stop_timestamp > covered_since,
                    Download.is_vod.is_(False),
                    Download.status.in_(_TAKEN_DOWNLOAD_STATUSES),
                )
            )
        ).all():
            covered[(row.account_id, row.channel_id)].append(
                _Covered(
                    account_id=row.account_id,
                    channel_id=row.channel_id,
                    title=exact_title_matcher.normalize(row.program_title),
                    start_timestamp=row.start_timestamp,
                    stop_timestamp=row.stop_timestamp,
                )
            )
        # The exact slot is taken whatever it is called: a provider correcting
        # a title in place must not look like a new airing.
        taken_slots = {
            (item.account_id, item.channel_id, item.start_timestamp, item.stop_timestamp)
            for items in covered.values()
            for item in items
        }

        pending_by_rule: dict[int, int] = defaultdict(int)
        for found_rule_id, count in (
            await session.execute(
                select(ScheduledRecording.recording_rule_id, func.count())
                .where(
                    ScheduledRecording.recording_rule_id.in_({rule.id for rule in rules}),
                    ScheduledRecording.status.in_(UNFIRED_SCHEDULE_STATUSES),
                )
                .group_by(ScheduledRecording.recording_rule_id)
            )
        ).all():
            pending_by_rule[found_rule_id] = int(count)

        rules_by_channel: dict[tuple[int, str], list[_Rule]] = defaultdict(list)
        for rule in rules:
            rules_by_channel[(rule.account_id, rule.channel_id)].append(rule)

        # Imported lazily to avoid coupling API module import order to service setup.
        # This is the same creation function used by POST /schedules and time slots.
        from api.schedules import ScheduleCreate, _create_schedule_record

        created = 0
        moved = 0
        capped: set[int] = set()
        for airing in airings:
            if airing.key in taken_keys or airing.slot in taken_slots:
                continue
            if airing.stop_timestamp <= earliest_stop_for(airing.account_id):
                # Finished, and either this is a refresh or it is too old.
                continue
            on_this_channel = covered[(airing.account_id, airing.channel_id)]
            same_showing = max(
                (item for item in on_this_channel if item.is_same_showing(airing)),
                key=lambda item: item.overlap_with(airing),
                default=None,
            )
            if same_showing is not None:
                # The provider moved a listing that is already dealt with.
                if same_showing.can_follow(airing) and await self._follow_listing(
                    session, same_showing, airing
                ):
                    moved += 1
                    taken_keys.add(airing.key)
                    taken_slots.add(airing.slot)
                continue
            for rule in rules_by_channel.get((airing.account_id, airing.channel_id), []):
                matcher = get_title_matcher(rule.match_mode)
                if not matcher.matches(airing.title[:MAX_MATCHED_TITLE_LENGTH], rule.title_match):
                    continue
                if rule.days_of_week is not None:
                    # Weekday is judged on the guide's wall clock, which is UTC
                    # shifted by the account guide offset and the global EPG
                    # offset (both applied by epg_service for display).
                    air_local = datetime.fromtimestamp(
                        airing.start_timestamp, tz=timezone.utc
                    ) + timedelta(
                        hours=account_offsets.get(airing.account_id, 0),
                        minutes=global_offset_minutes,
                    )
                    if air_local.weekday() not in rule.days_of_week:
                        continue
                if pending_by_rule[rule.id] >= MAX_PENDING_SCHEDULES_PER_RULE:
                    capped.add(rule.id)
                    continue

                auth = AuthContext(
                    authenticated=True,
                    role="download_only",
                    provider=rule.request_source,
                    user_id=rule.requested_by_user_id,
                    is_admin=rule.requested_by_user_id is None,
                )
                payload = ScheduleCreate(
                    account_id=airing.account_id,
                    channel_id=airing.channel_id,
                    channel_name=airing.channel_name,
                    program={
                        "id": airing.id,
                        "epg_id": airing.epg_id,
                        "title": airing.title,
                        "description": airing.description,
                        "category": airing.category,
                        "start_time": airing.start_time.isoformat(),
                        "end_time": airing.end_time.isoformat(),
                        "start_timestamp": airing.start_timestamp,
                        "stop_timestamp": airing.stop_timestamp,
                        "provider_start": airing.provider_start,
                        "provider_stop": airing.provider_stop,
                    },
                    pre_padding_minutes=rule.pre_padding_minutes,
                    post_padding_minutes=rule.post_padding_minutes,
                )
                try:
                    schedule = await _create_schedule_record(
                        payload,
                        auth,
                        session,
                        recording_rule_id=rule.id,
                        rule_airing_key=airing.key,
                        allow_ended=True,
                    )
                    created += 1
                    pending_by_rule[rule.id] += 1
                    taken_keys.add(airing.key)
                    taken_slots.add(airing.slot)
                    on_this_channel.append(
                        _Covered(
                            account_id=airing.account_id,
                            channel_id=airing.channel_id,
                            title=exact_title_matcher.normalize(airing.title),
                            start_timestamp=airing.start_timestamp,
                            stop_timestamp=airing.stop_timestamp,
                            schedule_id=schedule.id,
                            listing_id=airing.id,
                        )
                    )
                    break
                except HTTPException as exc:
                    if exc.status_code >= 500:
                        raise
                    # 409: a manual schedule or another rule already owns this
                    # airing. 400: the airing is no longer schedulable. Either
                    # way, skip it and keep evaluating the rest of the run.
                except IntegrityError:
                    # The unique airing key is the last line of defence when
                    # two processes evaluate at once. Treat the airing as
                    # taken and move on rather than abandoning the run.
                    await session.rollback()
                    taken_keys.add(airing.key)
                    break

        if moved:
            logger.info(
                "Moved %d waiting recording(s) to follow listings the provider retimed",
                moved,
            )
        for capped_rule_id in sorted(capped):
            logger.info(
                "Recording rule %s already has %d recordings waiting; the rest are "
                "scheduled as those are recorded",
                capped_rule_id,
                MAX_PENDING_SCHEDULES_PER_RULE,
            )
        return created

    async def _follow_listing(
        self, session: AsyncSession, waiting: _Covered, airing: _Airing
    ) -> bool:
        """Move a rule's waiting recording onto the listing the provider moved.

        Without this the recording keeps the old times and cuts off the end of
        a show that was pushed later. Only a recording that has not started is
        moved; the check is repeated in the UPDATE because the scheduler can
        start it between the read above and this write.
        """
        try:
            result = await session.execute(
                update(ScheduledRecording)
                .where(
                    ScheduledRecording.id == waiting.schedule_id,
                    ScheduledRecording.status.in_(UNFIRED_SCHEDULE_STATUSES),
                    ScheduledRecording.download_id.is_(None),
                )
                .values(
                    program_id=str(airing.id),
                    epg_id=airing.epg_id,
                    program_description=airing.description or None,
                    program_start=datetime.fromtimestamp(
                        airing.start_timestamp, tz=timezone.utc
                    ),
                    program_end=datetime.fromtimestamp(
                        airing.stop_timestamp, tz=timezone.utc
                    ),
                    start_timestamp=airing.start_timestamp,
                    stop_timestamp=airing.stop_timestamp,
                    provider_start=airing.provider_start,
                    provider_stop=airing.provider_stop,
                    duration_minutes=int(
                        (airing.stop_timestamp - airing.start_timestamp) / 60
                    ),
                    rule_airing_key=airing.key,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.commit()
        except IntegrityError:
            # Another recording already holds this listing's key.
            await session.rollback()
            return False
        if not result.rowcount:
            return False
        waiting.start_timestamp = airing.start_timestamp
        waiting.stop_timestamp = airing.stop_timestamp
        waiting.listing_id = airing.id
        return True


recording_rule_service = RecordingRuleService()
