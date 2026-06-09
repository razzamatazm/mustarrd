import base64
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import XtreamAccount, EPGProgram, AppSettings
from services.account_credentials import resolve_account_password_with_migration
from services.xtream_client import XtreamClient
from config import settings as app_settings


class NoCatchupSupportError(Exception):
    pass


class EPGService:
    def __init__(self):
        self._cache: dict = {}  # Simple in-memory cache
        self._cache_ttl = app_settings.epg_cache_ttl

    def _get_cache_key(
        self,
        account_id: int,
        channel_id: str,
        guide_offset_hours: int,
    ) -> str:
        return f"{account_id}:{channel_id}:{int(guide_offset_hours or 0)}"

    def _is_cache_valid(self, cache_key: str) -> bool:
        if cache_key not in self._cache:
            return False
        cached_at = self._cache[cache_key].get("cached_at")
        if not cached_at:
            return False
        return (datetime.utcnow() - cached_at).total_seconds() < self._cache_ttl

    def clear_cache(self):
        self._cache = {}

    async def _get_client(self, session: AsyncSession, account_id: int) -> XtreamClient:
        result = await session.execute(
            select(XtreamAccount).where(XtreamAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            raise Exception(f"Account {account_id} not found")

        password = await resolve_account_password_with_migration(session, account)
        return XtreamClient(account.server_url, account.username, password)

    @staticmethod
    def archive_days_for_channel(channel: dict) -> int:
        value = channel.get("tv_archive_duration")
        try:
            duration = int(value)
        except (TypeError, ValueError):
            return 0
        if duration <= 0:
            return 0
        if duration > 30:
            # Most providers report tv_archive_duration in days, but some send
            # hours (e.g. 168 for a 7-day archive). No provider offers more
            # than a 30-day archive, while hour-based providers send >= 24,
            # so anything above 30 is treated as hours.
            duration = max(1, duration // 24)
        return min(duration, 365)

    async def get_account_live_streams(
        self,
        session: AsyncSession,
        account_id: int,
    ) -> list:
        """Fetch the provider's full live channel list for an account (one API call)."""
        client = await self._get_client(session, account_id)
        try:
            return await client.get_live_streams()
        finally:
            await client.close()

    @classmethod
    def archive_days_from_channels(cls, channels: list, channel_id: str) -> int:
        """Resolve a channel's catchup window from an already-fetched channel list."""
        channel = next((ch for ch in channels if str(ch.get("stream_id")) == str(channel_id)), None)
        if not channel:
            raise NoCatchupSupportError(
                f"Channel {channel_id!r} was not found in the provider's channel list."
            )
        if int(channel.get("tv_archive", 0) or 0) != 1:
            raise NoCatchupSupportError(
                f"Channel {channel.get('name', channel_id)!r} does not support catchup recording."
            )
        return cls.archive_days_for_channel(channel)

    async def get_channel_archive_days(
        self,
        session: AsyncSession,
        account_id: int,
        channel_id: str,
    ) -> int:
        channels = await self.get_account_live_streams(session, account_id)
        return self.archive_days_from_channels(channels, channel_id)

    async def get_categories(self, session: AsyncSession, account_id: int) -> list:
        """Get channel categories for an account."""
        client = await self._get_client(session, account_id)
        try:
            return await client.get_live_categories()
        finally:
            await client.close()

    async def get_channels(
        self,
        session: AsyncSession,
        account_id: int,
        category_id: Optional[str] = None
    ) -> list:
        """Get channels for an account, optionally filtered by category."""
        client = await self._get_client(session, account_id)
        try:
            channels = await client.get_live_streams(category_id)
            # Filter to only channels with catchup enabled
            return [ch for ch in channels if int(ch.get("tv_archive", 0) or 0) == 1]
        finally:
            await client.close()

    async def get_epg_for_channel(
        self,
        session: AsyncSession,
        account_id: int,
        channel_id: str,
        use_cache: bool = True,
        prefer_live: bool = False,
        days_back: Optional[int] = None,
        archive_days: Optional[int] = None,
    ) -> list:
        """Get EPG data for a specific channel.

        *archive_days*: pass the value from an earlier get_channel_archive_days
        call to skip the internal lookup, which fetches the provider's entire
        channel list. Callers that already resolved it (e.g. the Browse EPG and
        Catchup endpoints) would otherwise trigger that fetch twice per request.
        """
        account_result = await session.execute(
            select(XtreamAccount).where(XtreamAccount.id == account_id)
        )
        account = account_result.scalar_one_or_none()
        if not account:
            raise Exception(f"Account {account_id} not found")

        settings_result = await session.execute(select(AppSettings))
        db_settings = settings_result.scalar_one_or_none()
        global_offset_minutes = int(getattr(db_settings, "epg_offset_minutes", 0) or 0)

        cache_key = self._get_cache_key(
            account_id,
            channel_id,
            account.guide_offset_hours or 0,
        )

        if use_cache and not prefer_live and self._is_cache_valid(cache_key):
            return self._cache[cache_key]["data"]

        if archive_days is None:
            try:
                archive_days = await self.get_channel_archive_days(session, account_id, channel_id)
            except NoCatchupSupportError:
                return []
        if archive_days <= 0:
            return []
        if days_back is None:
            days_back = archive_days
        else:
            days_back = min(days_back, archive_days)

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        async def fetch_live_epg() -> list:
            client = await self._get_client(session, account_id)
            try:
                epg_data = await client.get_epg(channel_id)
                processed = [
                    self._process_epg_entry(
                        entry, account,
                        fallback_channel_id=channel_id,
                        has_archive_fallback=True,
                        global_offset_minutes=global_offset_minutes,
                    )
                    for entry in epg_data
                ]
                filtered = self._filter_programs_by_cutoff(processed, cutoff)
                return self._dedupe_programs(filtered)
            finally:
                await client.close()

        if prefer_live:
            try:
                live_programs = await fetch_live_epg()
            except Exception:
                live_programs = []
            if live_programs:
                self._cache[cache_key] = {
                    "data": live_programs,
                    "cached_at": datetime.utcnow()
                }
                return live_programs

        db_result = await session.execute(
            select(EPGProgram)
            .where(
                EPGProgram.account_id == account_id,
                EPGProgram.channel_id == channel_id,
                EPGProgram.end_time >= cutoff,
            )
            .order_by(EPGProgram.start_time.desc())
        )
        db_rows = db_result.scalars().all()
        if db_rows:
            processed = [
                self.serialize_program(row, account, global_offset_minutes)
                for row in db_rows
            ]
            processed = self._dedupe_programs(processed)
            self._cache[cache_key] = {
                "data": processed,
                "cached_at": datetime.utcnow()
            }
            return processed

        # Fallback to live API if DB has no data
        live_programs = await fetch_live_epg()
        self._cache[cache_key] = {
            "data": live_programs,
            "cached_at": datetime.utcnow()
        }
        return live_programs

    def _process_epg_entry(
        self,
        entry: dict,
        account: Optional[XtreamAccount] = None,
        fallback_channel_id: Optional[str] = None,
        has_archive_fallback: bool = False,
        global_offset_minutes: int = 0,
    ) -> dict:
        """Process and normalize an EPG entry."""
        # Decode base64 title and description if present
        title = entry.get("title", "")
        if title:
            try:
                title = base64.b64decode(title).decode("utf-8")
            except Exception:
                pass

        description = entry.get("description", "")
        if description:
            try:
                description = base64.b64decode(description).decode("utf-8")
            except Exception:
                pass

        # Parse timestamps
        start_timestamp = self._coerce_timestamp(entry.get("start_timestamp"))
        stop_timestamp = self._coerce_timestamp(entry.get("stop_timestamp"))

        start_time_utc = datetime.fromtimestamp(start_timestamp, tz=timezone.utc) if start_timestamp else None
        end_time_utc = datetime.fromtimestamp(stop_timestamp, tz=timezone.utc) if stop_timestamp else None

        provider_start = entry.get("start")
        provider_stop = entry.get("stop")
        start_time, end_time = self._resolve_display_window(
            start_time_utc, end_time_utc, account, global_offset_minutes
        )

        duration_minutes = 0
        if start_time and end_time:
            duration_minutes = int((end_time - start_time).total_seconds() / 60)

        channel_id = str(entry.get("channel_id") or fallback_channel_id or "")
        epg_id = entry.get("epg_id")
        if channel_id and start_timestamp and stop_timestamp:
            epg_id = f"{channel_id}:{start_timestamp}:{stop_timestamp}"

        raw_has_archive = entry.get("has_archive")

        return {
            "id": entry.get("id"),
            "epg_id": epg_id,
            "title": title,
            "description": description,
            "start_time": start_time.isoformat() if start_time else None,
            "end_time": end_time.isoformat() if end_time else None,
            "start_timestamp": start_timestamp,
            "stop_timestamp": stop_timestamp,
            "provider_start": str(provider_start).strip() if provider_start is not None else None,
            "provider_stop": str(provider_stop).strip() if provider_stop is not None else None,
            "duration_minutes": duration_minutes,
            "has_archive": has_archive_fallback if raw_has_archive is None else (int(raw_has_archive or 0) == 1),
            "channel_id": channel_id or None,
        }

    async def get_past_programs(
        self,
        session: AsyncSession,
        account_id: int,
        channel_id: str,
        days_back: int = 7,
        archive_days: Optional[int] = None,
    ) -> list:
        """Get past programs that are available for catchup."""
        epg_data = await self.get_epg_for_channel(
            session,
            account_id,
            channel_id,
            days_back=days_back,
            archive_days=archive_days,
        )

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days_back)

        past_programs = []
        for program in epg_data:
            start_ts = self._coerce_timestamp(program.get("start_timestamp"))
            stop_ts = self._coerce_timestamp(program.get("stop_timestamp"))
            if not start_ts or not stop_ts:
                continue

            start_time = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            end_time = datetime.fromtimestamp(stop_ts, tz=timezone.utc)

            # Include programs that ended within the catchup window. Using end_time
            # (not start_time) means a long program that started just before the
            # archive boundary but ended within it is still shown and downloadable.
            if end_time > cutoff and end_time < now:
                if program.get("has_archive", False):
                    past_programs.append(program)

        # Sort by start time, most recent first
        past_programs.sort(key=lambda x: self._coerce_timestamp(x.get("start_timestamp")), reverse=True)
        return past_programs

    def serialize_program(
        self,
        row: EPGProgram,
        account: Optional[XtreamAccount] = None,
        global_offset_minutes: int = 0,
    ) -> dict:
        start_time, end_time = self._resolve_display_window(
            row.start_time, row.end_time, account, global_offset_minutes
        )
        duration_minutes = 0
        if start_time and end_time:
            duration_minutes = int((end_time - start_time).total_seconds() / 60)

        return {
            "id": row.id,
            "epg_id": row.epg_id,
            "title": row.title,
            "description": row.description,
            "start_time": start_time.isoformat() if start_time else None,
            "end_time": end_time.isoformat() if end_time else None,
            "start_timestamp": row.start_timestamp,
            "stop_timestamp": row.stop_timestamp,
            "provider_start": row.provider_start,
            "provider_stop": row.provider_stop,
            "duration_minutes": duration_minutes,
            "has_archive": row.has_archive,
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "category": row.category,
        }

    def _filter_programs_by_cutoff(self, programs: list[dict], cutoff: datetime) -> list:
        filtered: list[dict] = []
        for program in programs:
            stop_ts = self._coerce_timestamp(program.get("stop_timestamp"))
            if not stop_ts:
                continue
            end_time = datetime.fromtimestamp(stop_ts, tz=timezone.utc)
            if end_time >= cutoff:
                filtered.append(program)
        return filtered

    def _normalize_time(self, dt_value: Optional[datetime]) -> Optional[datetime]:
        if not dt_value:
            return None
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)

    def _resolve_display_window(
        self,
        start_time_utc: Optional[datetime],
        end_time_utc: Optional[datetime],
        account: Optional[XtreamAccount],
        global_offset_minutes: int = 0,
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        normalized_start = self._normalize_time(start_time_utc)
        normalized_end = self._normalize_time(end_time_utc)
        total_offset = timedelta(
            hours=int(getattr(account, "guide_offset_hours", 0) or 0),
            minutes=global_offset_minutes,
        )
        if total_offset:
            if normalized_start:
                normalized_start = normalized_start + total_offset
            if normalized_end:
                normalized_end = normalized_end + total_offset
        return normalized_start, normalized_end

    def _coerce_timestamp(self, value) -> int:
        if value is None:
            return 0
        try:
            result = int(value)
        except (TypeError, ValueError):
            try:
                result = int(float(value))
            except (TypeError, ValueError):
                return 0
        if result > 10_000_000_000:
            result = result // 1000
        return result

    def _extract_program_timestamps(self, program: dict) -> tuple[int, int]:
        start_ts = self._coerce_timestamp(program.get("start_timestamp"))
        stop_ts = self._coerce_timestamp(program.get("stop_timestamp"))
        if start_ts and stop_ts:
            return start_ts, stop_ts

        start_iso = program.get("start_time")
        end_iso = program.get("end_time")
        try:
            if start_iso and end_iso:
                start_dt = datetime.fromisoformat(start_iso)
                end_dt = datetime.fromisoformat(end_iso)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                return int(start_dt.timestamp()), int(end_dt.timestamp())
        except (TypeError, ValueError):
            pass

        return 0, 0

    def _dedupe_programs(self, programs: list[dict]) -> list[dict]:
        deduped: dict[tuple, dict] = {}

        for program in programs:
            channel_id = str(program.get("channel_id") or "")
            start_ts, stop_ts = self._extract_program_timestamps(program)
            title_key = (program.get("title") or "").strip().lower()

            if channel_id and start_ts and stop_ts:
                key = (channel_id, start_ts, stop_ts)
            else:
                key = (channel_id, program.get("start_time"), program.get("end_time"), title_key)

            existing = deduped.get(key)
            if not existing:
                deduped[key] = program
                continue

            existing_score = (
                1 if existing.get("has_archive") else 0,
                len(existing.get("description") or ""),
                1 if existing.get("epg_id") else 0,
            )
            program_score = (
                1 if program.get("has_archive") else 0,
                len(program.get("description") or ""),
                1 if program.get("epg_id") else 0,
            )
            if program_score > existing_score:
                deduped[key] = program

        return list(deduped.values())

    def detect_program_type(self, program: dict, channel: dict = None) -> str:
        """
        Analyze program metadata to determine content type.

        Returns: 'tv_show', 'movie', 'sports', 'news', or 'other'
        """
        title = program.get("title", "").lower()
        description = program.get("description", "").lower()
        category = ""
        if channel:
            category = channel.get("category_name", "").lower()

        # Check for TV show patterns (season/episode)
        import re
        if re.search(r's\d{1,2}e\d{1,2}|season\s*\d+|episode\s*\d+', title + description, re.IGNORECASE):
            return "tv_show"

        # Check for sports
        sports_keywords = ['vs', 'vs.', ' @ ', 'nfl', 'nba', 'nhl', 'mlb', 'ufc', 'boxing',
                          'soccer', 'football', 'basketball', 'hockey', 'baseball', 'tennis',
                          'golf', 'racing', 'motorsport', 'wrestling', 'mma']
        sports_categories = ['sports', 'deportes', 'sport', 'esports']

        if any(kw in title for kw in sports_keywords) or \
           any(cat in category for cat in sports_categories):
            return "sports"

        # Check for movies
        movie_categories = ['movie', 'movies', 'film', 'films', 'cinema', 'peliculas']
        if any(cat in category for cat in movie_categories):
            return "movie"

        # Check for news
        news_keywords = ['news', 'noticias', 'journal', 'breaking']
        news_categories = ['news', 'noticias', 'information']
        if any(kw in title for kw in news_keywords) or \
           any(cat in category for cat in news_categories):
            return "news"

        return "other"


# Global instance
epg_service = EPGService()
