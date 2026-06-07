import asyncio
import base64
import gzip
import io
import logging
import zlib
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET
from sqlalchemy import delete, insert, select, func

from config import settings as app_settings
from database import async_session_maker
from models import EPGProgram, XtreamAccount
from services.account_credentials import resolve_account_password
from services.xtream_client import XtreamClient
from services.epg_service import epg_service
from services.log_stream import backend_log_stream

logger = logging.getLogger(__name__)


def _friendly_connection_error(e: Exception) -> str:
    """Translate a connection exception into a short, user-readable string."""
    import aiohttp
    msg = str(e)
    if isinstance(e, asyncio.TimeoutError):
        return "Connection timed out."
    if isinstance(e, aiohttp.ClientConnectorError):
        return "Cannot reach provider. Check the server URL."
    if isinstance(e, aiohttp.ClientError):
        return "Network error. Try again later."
    if "Invalid credentials" in msg:
        return "Invalid credentials. Check your username and password."
    if "Authentication failed" in msg or "HTTP 401" in msg or "HTTP 403" in msg:
        return "Provider rejected login. Check your username and password."
    if "HTTP 5" in msg:
        return "Provider server error. Try again later."
    # Truncate raw fallthrough so a stray library exception cannot store a wall of
    # text or a URL-carrying error string in the card.
    return (msg[:200] + "...") if len(msg) > 200 else (msg or "Connection failed.")


class EPGIngestManager:
    def __init__(self):
        self._running = False
        self._refresh_lock = asyncio.Lock()
        self._interval = max(1, int(app_settings.epg_refresh_interval_hours)) * 3600
        self._backfill_cooldown_seconds = max(self._interval, 6 * 3600)
        self._status = {
            "running": False,
            "account_id": None,
            "account_name": None,
            "processed_programs": 0,
            "inserted_programs": 0,
            "total_programs": None,
            "started_at": None,
            "last_completed_at": None,
            "last_error": None,
        }

    async def _log(self, message: str, level: str = "info", account: Optional[XtreamAccount] = None):
        await backend_log_stream.emit(
            source="epg",
            message=message,
            level=level,
            account_id=getattr(account, "id", None),
            account_name=getattr(account, "name", None),
        )

    async def process_queue(self):
        self._running = True
        await self._log("EPG refresh loop started.")
        while self._running:
            try:
                await self.refresh_all_accounts()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error refreshing XMLTV")
                await self._log(f"EPG refresh loop error: {exc}", level="error")
            await asyncio.sleep(self._interval)

    async def refresh_all_accounts(self, force: bool = False):
        async with self._refresh_lock:
            if force:
                await self._log("Starting forced full EPG refresh across active accounts.")
            else:
                await self._log("Starting full EPG refresh across active accounts.")
            await self._refresh_all_accounts(force=force)

    async def refresh_account_by_id(self, account_id: int, force: bool = False):
        async with self._refresh_lock:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(XtreamAccount).where(XtreamAccount.id == account_id)
                )
                account = result.scalar_one_or_none()

            if not account:
                raise ValueError(f"Account {account_id} not found")

            try:
                if force:
                    await self._log("Starting forced account-specific EPG refresh.", account=account)
                else:
                    await self._log("Starting account-specific EPG refresh.", account=account)
                await self._refresh_account(account, force=force)
                await self._update_connection_status(account.id, ok=True)
            except Exception as exc:
                self._status["last_error"] = str(exc)
                await self._log(f"Account EPG refresh failed: {exc}", level="error", account=account)
                await self._update_connection_status(account.id, ok=False, error=_friendly_connection_error(exc))
                raise
            finally:
                self._status.update({
                    "running": False,
                    "last_completed_at": datetime.now(timezone.utc),
                })
                await self._log("Account-specific EPG refresh finished.", account=account)

    def get_status(self) -> dict:
        status = dict(self._status)
        for key in ("started_at", "last_completed_at"):
            if status.get(key):
                status[key] = status[key].isoformat()
        return status

    async def _refresh_all_accounts(self, force: bool = False):
        async with async_session_maker() as session:
            result = await session.execute(
                select(XtreamAccount).where(XtreamAccount.is_active == True)  # noqa: E712
            )
            accounts = result.scalars().all()

        self._status.update({
            "running": True,
            "account_id": None,
            "account_name": None,
            "processed_programs": 0,
            "inserted_programs": 0,
            "total_programs": None,
            "started_at": datetime.now(timezone.utc),
            "last_error": None,
        })

        if not accounts:
            await self._log("No active accounts found for EPG refresh.")
            self._status.update({
                "running": False,
                "last_completed_at": datetime.now(timezone.utc),
            })
            return

        for account in accounts:
            try:
                await self._refresh_account(account, force=force)
                await self._update_connection_status(account.id, ok=True)
            except Exception as exc:
                self._status.update({
                    "last_error": str(exc),
                })
                logger.exception("Error refreshing XMLTV for account %s", account.id)
                await self._log(f"EPG refresh failed: {exc}", level="error", account=account)
                await self._update_connection_status(account.id, ok=False, error=_friendly_connection_error(exc))

        self._status.update({
            "running": False,
            "last_completed_at": datetime.now(timezone.utc),
        })
        await self._log("Full EPG refresh finished.")

    async def _refresh_account(self, account: XtreamAccount, force: bool = False):
        self._status.update({
            "running": True,
            "account_id": account.id,
            "account_name": account.name,
            "processed_programs": 0,
            "inserted_programs": 0,
            "total_programs": None,
            "started_at": datetime.now(timezone.utc),
            "last_error": None,
        })
        await self._log("Refreshing EPG for account.", account=account)
        processed = 0
        inserted = 0
        password = resolve_account_password(account)
        client = XtreamClient(account.server_url, account.username, password)
        insert_stmt = insert(EPGProgram).prefix_with("OR IGNORE")
        try:
            channels = await client.get_live_streams()
            catchup_channels = [
                ch for ch in channels
                if int(ch.get("tv_archive", 0) or 0) == 1
            ]
            await self._log(
                f"Found {len(catchup_channels)} catchup-enabled channels.",
                account=account
            )
            channel_maps = self._build_channel_maps(catchup_channels)

            raw_xmltv = await client.get_xmltv()
            xmltv_bytes = self._maybe_decompress(raw_xmltv) if raw_xmltv else b""
            total_programs = xmltv_bytes.count(b"<programme") if xmltv_bytes else 0
            self._status["total_programs"] = total_programs if total_programs > 0 else None
            if xmltv_bytes:
                await self._log(
                    f"XMLTV downloaded ({len(xmltv_bytes):,} bytes, {total_programs:,} programme tags).",
                    account=account
                )
            else:
                await self._log("XMLTV response was empty.", level="warning", account=account)

            now_utc = datetime.now(timezone.utc)
            earliest_start_by_channel: dict[str, datetime] = {}

            async with async_session_maker() as session:
                async with session.begin():
                    if force:
                        await self._log(
                            "Force mode enabled: clearing existing guide rows before reload.",
                            account=account,
                        )
                        await session.execute(
                            delete(EPGProgram).where(
                                EPGProgram.account_id == account.id,
                            )
                        )
                    else:
                        for stream_id, info in channel_maps["stream_info"].items():
                            archive_days = int(info.get("archive_days") or 0)
                            if archive_days <= 0:
                                # archive window unknown: preserve existing rows
                                continue
                            channel_cutoff = now_utc - timedelta(days=archive_days)
                            await session.execute(
                                delete(EPGProgram).where(
                                    EPGProgram.account_id == account.id,
                                    EPGProgram.channel_id == stream_id,
                                    EPGProgram.end_time < channel_cutoff,
                                )
                            )

                async with session.begin():
                    earliest_rows = await session.execute(
                        select(
                            EPGProgram.channel_id,
                            func.min(EPGProgram.start_time),
                        ).where(
                            EPGProgram.account_id == account.id
                        ).group_by(EPGProgram.channel_id)
                    )
                    for channel_id, earliest_start in earliest_rows.all():
                        channel_key = str(channel_id)
                        aware_start = self._ensure_aware(earliest_start)
                        if aware_start:
                            earliest_start_by_channel[channel_key] = aware_start

                async def flush_batch(batch: list[dict]) -> tuple[list[dict], int]:
                    nonlocal inserted
                    if not batch:
                        return batch, inserted
                    async with session.begin():
                        sqlite_before_changes = await self._get_sqlite_total_changes(session)
                        result = await session.execute(insert_stmt, batch)
                        rowcount = await self._get_inserted_rowcount(
                            session,
                            result,
                            sqlite_before_changes=sqlite_before_changes,
                        )
                    if rowcount > 0:
                        inserted += rowcount
                    return [], inserted

                if xmltv_bytes:
                    program_iter = self._iter_programs(
                        xmltv_bytes,
                        channel_maps,
                        now_utc,
                    )

                    batch: list[dict] = []
                    for program in program_iter:
                        stream_id = str(program["channel_id"])
                        start_time = self._ensure_aware(program["start_time"])
                        if start_time:
                            existing_earliest = earliest_start_by_channel.get(stream_id)
                            if existing_earliest is None or start_time < existing_earliest:
                                earliest_start_by_channel[stream_id] = start_time
                        program["account_id"] = account.id
                        batch.append(program)
                        processed += 1
                        if processed % 500 == 0:
                            self._status["processed_programs"] = processed
                            self._status["inserted_programs"] = inserted
                        if processed % 5000 == 0:
                            await self._log(
                                f"Parsed {processed:,} EPG entries so far.",
                                account=account
                            )
                        if len(batch) >= 1000:
                            batch, inserted = await flush_batch(batch)

                    if batch:
                        batch, inserted = await flush_batch(batch)

            backfill_targets: list[tuple[dict, datetime, int]] = []
            for channel in catchup_channels:
                stream_id = str(channel.get("stream_id"))
                if not stream_id:
                    continue
                archive_days = epg_service.archive_days_for_channel(channel)
                if archive_days <= 0:
                    continue
                channel_cutoff = now_utc - timedelta(days=archive_days)
                channel_earliest = earliest_start_by_channel.get(stream_id)
                if channel_earliest is None:
                    backfill_targets.append((channel, now_utc, archive_days))
                    continue
                if channel_earliest > channel_cutoff:
                    backfill_targets.append((channel, channel_earliest, archive_days))

            should_backfill = force or self._should_backfill(account, now_utc)
            if backfill_targets and should_backfill:
                self._status["total_programs"] = None
                await self._log(
                    f"Starting API backfill for {len(backfill_targets):,} channels.",
                    account=account
                )
                processed, inserted = await self._backfill_from_api(
                    client=client,
                    channel_targets=backfill_targets,
                    now_utc=now_utc,
                    processed=processed,
                    inserted=inserted,
                    account_id=account.id,
                    insert_stmt=insert_stmt,
                )
                await self._mark_backfill_attempt(account.id, now_utc)
            elif backfill_targets:
                await self._log(
                    "Skipping API backfill (cooldown active).",
                    account=account
                )
            elif not xmltv_bytes and not earliest_start_by_channel:
                self._status["last_error"] = "No XMLTV data returned by provider."
                await self._log(
                    "No XMLTV data returned by provider; no EPG rows were refreshed.",
                    level="warning",
                    account=account
                )

        finally:
            await client.close()

        epg_service.clear_cache()
        self._status.update({
            "processed_programs": processed,
            "inserted_programs": inserted,
            "last_completed_at": datetime.now(timezone.utc),
        })
        await self._log(
            f"EPG refresh complete. Parsed {processed:,} entries; inserted {inserted:,} new rows.",
            account=account
        )

    async def _update_connection_status(self, account_id: int, ok: bool, error: str | None = None) -> None:
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(XtreamAccount).where(XtreamAccount.id == account_id)
                )
                account = result.scalar_one_or_none()
                if account:
                    account.last_connection_ok = ok
                    account.last_connection_checked_at = datetime.now(timezone.utc)
                    account.last_connection_error = error if not ok else None
                    await session.commit()
        except Exception:
            logger.exception("Failed to update connection status for account %s", account_id)

    def _build_channel_maps(self, channels: list[dict]) -> dict:
        stream_by_xmltv_id: Dict[str, str] = {}
        stream_info: Dict[str, dict] = {}

        # Separate channels into two buckets so that name-only channels
        # (no explicit xmltv id) get priority in the name fallback slot.
        # A channel with an explicit id is already reachable via
        # stream_by_xmltv_id, so it should only occupy the name slot when no
        # name-only channel shares that normalized name.
        name_only_names: list[tuple[str, str]] = []  # (norm_name, stream_id)
        has_id_names: list[tuple[str, str]] = []

        for ch in channels:
            stream_id = str(ch.get("stream_id"))
            name = (ch.get("name") or "").strip()
            stream_info[stream_id] = {
                "name": name or stream_id,
                "has_archive": int(ch.get("tv_archive", 0) or 0) == 1,
                "archive_days": epg_service.archive_days_for_channel(ch),
            }

            xmltv_id = self._extract_xmltv_id(ch)
            if xmltv_id:
                stream_by_xmltv_id[str(xmltv_id)] = stream_id
                if name:
                    has_id_names.append((self._normalize_name(name), stream_id))
            else:
                if name:
                    name_only_names.append((self._normalize_name(name), stream_id))

        # Pass 1: name-only channels fill the name slot (first-write-wins).
        stream_by_name: Dict[str, str] = {}
        for norm_name, stream_id in name_only_names:
            stream_by_name.setdefault(norm_name, stream_id)
        # Pass 2: id-having channels fill any remaining gaps only.
        for norm_name, stream_id in has_id_names:
            stream_by_name.setdefault(norm_name, stream_id)

        return {
            "stream_by_xmltv_id": stream_by_xmltv_id,
            "stream_by_name": stream_by_name,
            "stream_info": stream_info,
        }

    def _iter_programs(
        self,
        xmltv_bytes: bytes,
        channel_maps: dict,
        now_utc: datetime,
    ) -> Iterable[dict]:
        stream_by_xmltv_id = channel_maps["stream_by_xmltv_id"]
        stream_by_name = channel_maps["stream_by_name"]
        stream_info = channel_maps["stream_info"]
        xmltv_to_stream: Dict[str, str] = dict(stream_by_xmltv_id)

        for _, elem in ET.iterparse(io.BytesIO(xmltv_bytes), events=("end",)):
            if elem.tag == "channel":
                xmltv_id = elem.get("id")
                display_name = self._extract_text(elem, "display-name")
                if xmltv_id and xmltv_id not in xmltv_to_stream:
                    if xmltv_id in stream_info:
                        xmltv_to_stream[xmltv_id] = xmltv_id
                    elif display_name:
                        name_key = self._normalize_name(display_name)
                        stream_id = stream_by_name.get(name_key)
                        if stream_id:
                            xmltv_to_stream[xmltv_id] = stream_id
                elem.clear()
                continue

            if elem.tag != "programme":
                continue

            xmltv_id = elem.get("channel")
            if not xmltv_id:
                elem.clear()
                continue

            stream_id = xmltv_to_stream.get(xmltv_id)
            if not stream_id or stream_id not in stream_info:
                elem.clear()
                continue

            start_raw = elem.get("start")
            stop_raw = elem.get("stop")
            start_dt = self._parse_xmltv_time(start_raw)
            end_dt = self._parse_xmltv_time(stop_raw)
            if not start_dt or not end_dt:
                elem.clear()
                continue

            start_utc = start_dt.astimezone(timezone.utc)
            end_utc = end_dt.astimezone(timezone.utc)
            archive_days = int(stream_info[stream_id].get("archive_days") or 0)
            if archive_days > 0:
                channel_cutoff = now_utc - timedelta(days=archive_days)
                if end_utc < channel_cutoff:
                    elem.clear()
                    continue

            duration_minutes = int((end_utc - start_utc).total_seconds() / 60)
            if duration_minutes <= 0:
                elem.clear()
                continue

            title = self._extract_text(elem, "title") or "Unknown"
            description = self._extract_text(elem, "desc")
            category = self._extract_text(elem, "category")

            start_ts = int(start_utc.timestamp())
            stop_ts = int(end_utc.timestamp())
            epg_id = f"{stream_id}:{start_ts}:{stop_ts}"

            info = stream_info[stream_id]
            yield {
                "channel_id": stream_id,
                "channel_name": info["name"],
                "xmltv_id": xmltv_id,
                "epg_id": epg_id,
                "title": title,
                "description": description,
                "category": category,
                "start_time": start_utc,
                "end_time": end_utc,
                "start_timestamp": start_ts,
                "stop_timestamp": stop_ts,
                "provider_start": start_raw,
                "provider_stop": stop_raw,
                "duration_minutes": duration_minutes,
                "has_archive": info["has_archive"],
            }

            elem.clear()

    def _maybe_decompress(self, data: bytes) -> bytes:
        if data[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(data)
            except (gzip.BadGzipFile, EOFError, OSError, zlib.error):
                return b""
        return data

    def _ensure_aware(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _parse_xmltv_time(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        value = value.strip()
        if len(value) >= 14:
            date_part = value[:14]
            tz_part = value[14:].strip()
            try:
                dt = datetime.strptime(date_part, "%Y%m%d%H%M%S")
            except ValueError:
                return None
            if tz_part:
                try:
                    if tz_part[0] not in "+-":
                        tz_part = tz_part.replace(" ", "")
                    if tz_part.upper() == "Z":
                        return dt.replace(tzinfo=timezone.utc)
                    if len(tz_part) == 6 and tz_part[3] == ":":
                        tz_part = tz_part.replace(":", "")
                    offset = datetime.strptime(tz_part, "%z").tzinfo
                    return dt.replace(tzinfo=offset)
                except ValueError:
                    return dt.replace(tzinfo=timezone.utc)
            return dt.replace(tzinfo=timezone.utc)
        return None

    def _extract_text(self, elem: Element, tag: str) -> Optional[str]:
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return None

    def _extract_xmltv_id(self, channel: dict) -> Optional[str]:
        for key in ("epg_channel_id", "tvg_id", "tvgid", "tvg_name", "tvg-id"):
            value = channel.get(key)
            if value:
                return str(value).strip()
        return None

    def _normalize_name(self, name: str) -> str:
        return " ".join(name.lower().split())

    def _decode_base64_text(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return value

    async def _get_inserted_rowcount(
        self,
        session,
        result,
        sqlite_before_changes: Optional[int] = None,
    ) -> int:
        rowcount = getattr(result, "rowcount", None)
        if rowcount is None:
            raw_result = getattr(result, "raw", None)
            if raw_result is not None:
                rowcount = getattr(raw_result, "rowcount", None)

        if rowcount is None:
            rowcount = 0

        try:
            rowcount = int(rowcount)
        except (TypeError, ValueError):
            rowcount = 0

        if rowcount > 0:
            return rowcount

        if sqlite_before_changes is None:
            return 0

        sqlite_after_changes = await self._get_sqlite_total_changes(session)
        if sqlite_after_changes is None:
            return 0

        delta = sqlite_after_changes - sqlite_before_changes
        return delta if delta > 0 else 0

    async def _get_sqlite_total_changes(self, session) -> Optional[int]:
        bind = session.get_bind()
        if not bind or bind.dialect.name != "sqlite":
            return None

        try:
            result = await session.execute(select(func.total_changes()))
            total_changes = result.scalar_one_or_none()
            if total_changes is None:
                return None
            return int(total_changes)
        except Exception:
            return None

    def _parse_timestamp(self, value) -> Optional[int]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp = int(timestamp / 1000)
            return timestamp

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            timestamp = int(text)
            if timestamp > 10_000_000_000:
                timestamp = int(timestamp / 1000)
            return timestamp

        try:
            timestamp = int(float(text))
            if timestamp > 10_000_000_000:
                timestamp = int(timestamp / 1000)
            return timestamp
        except (TypeError, ValueError):
            pass

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.astimezone(timezone.utc).timestamp())
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                return int(parsed.timestamp())
            except ValueError:
                continue

        return None

    def _bool_from_value(self, value, fallback: bool = False) -> bool:
        if value is None:
            return fallback
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return int(value) == 1

        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return fallback

    def _should_backfill(self, account: XtreamAccount, now_utc: datetime) -> bool:
        last_backfill = self._ensure_aware(account.last_epg_backfill_at)
        if not last_backfill:
            return True
        return (now_utc - last_backfill).total_seconds() >= self._backfill_cooldown_seconds

    async def _mark_backfill_attempt(self, account_id: int, attempted_at: datetime) -> None:
        async with async_session_maker() as session:
            async with session.begin():
                result = await session.execute(
                    select(XtreamAccount).where(XtreamAccount.id == account_id)
                )
                account = result.scalar_one_or_none()
                if account:
                    account.last_epg_backfill_at = attempted_at

    async def _backfill_from_api(
        self,
        client: XtreamClient,
        channel_targets: list[tuple[dict, datetime, int]],
        now_utc: datetime,
        processed: int,
        inserted: int,
        account_id: int,
        insert_stmt,
    ) -> tuple[int, int]:
        async with async_session_maker() as session:
            batch: list[dict] = []

            async def flush_batch():
                nonlocal batch, inserted
                if not batch:
                    return
                async with session.begin():
                    sqlite_before_changes = await self._get_sqlite_total_changes(session)
                    result = await session.execute(insert_stmt, batch)
                    rowcount = await self._get_inserted_rowcount(
                        session,
                        result,
                        sqlite_before_changes=sqlite_before_changes,
                    )
                if rowcount > 0:
                    inserted += rowcount
                batch = []

            for index, (channel, backfill_end, archive_days) in enumerate(channel_targets, start=1):
                stream_id = str(channel.get("stream_id"))
                if not stream_id:
                    continue
                if archive_days <= 0:
                    continue
                channel_name = (channel.get("name") or stream_id).strip()
                xmltv_id = self._extract_xmltv_id(channel)
                channel_has_archive = self._bool_from_value(channel.get("tv_archive"), fallback=True)
                backfill_end = self._ensure_aware(backfill_end) or datetime.now(timezone.utc)
                channel_cutoff = now_utc - timedelta(days=archive_days)

                try:
                    epg_entries = await client.get_epg(stream_id)
                except Exception as exc:
                    self._status["last_error"] = f"EPG fetch failed for channel {stream_id}: {exc}"
                    await self._log(
                        f"Backfill failed for channel {stream_id}: {exc}",
                        level="warning",
                    )
                    continue

                for entry in epg_entries:
                    start_ts = self._parse_timestamp(entry.get("start_timestamp"))
                    stop_ts = self._parse_timestamp(entry.get("stop_timestamp"))
                    if start_ts is None:
                        start_ts = self._parse_timestamp(entry.get("start"))
                    if stop_ts is None:
                        stop_ts = self._parse_timestamp(entry.get("stop"))
                    if not start_ts or not stop_ts:
                        continue

                    start_utc = datetime.fromtimestamp(start_ts, tz=timezone.utc)
                    end_utc = datetime.fromtimestamp(stop_ts, tz=timezone.utc)
                    if end_utc < channel_cutoff or start_utc >= backfill_end:
                        continue

                    duration_minutes = int((end_utc - start_utc).total_seconds() / 60)
                    if duration_minutes <= 0:
                        continue

                    title = self._decode_base64_text(entry.get("title")) or "Unknown"
                    description = self._decode_base64_text(entry.get("description"))
                    category = entry.get("category")
                    epg_id = f"{stream_id}:{start_ts}:{stop_ts}"
                    has_archive = self._bool_from_value(entry.get("has_archive"), fallback=channel_has_archive)

                    batch.append({
                        "account_id": account_id,
                        "channel_id": stream_id,
                        "channel_name": channel_name or stream_id,
                        "xmltv_id": xmltv_id,
                        "epg_id": str(epg_id),
                        "title": title,
                        "description": description,
                        "category": category,
                        "start_time": start_utc,
                        "end_time": end_utc,
                        "start_timestamp": start_ts,
                        "stop_timestamp": stop_ts,
                        "provider_start": str(entry.get("start")).strip() if entry.get("start") is not None else None,
                        "provider_stop": str(entry.get("stop")).strip() if entry.get("stop") is not None else None,
                        "duration_minutes": duration_minutes,
                        "has_archive": has_archive,
                    })
                    processed += 1
                    if processed % 500 == 0:
                        self._status["processed_programs"] = processed
                        self._status["inserted_programs"] = inserted
                    if processed % 5000 == 0:
                        await self._log(
                            f"Backfill progress: {processed:,} entries processed."
                        )
                    if len(batch) >= 1000:
                        await flush_batch()

                if index % 100 == 0:
                    await self._log(
                        f"Backfill checked {index:,}/{len(channel_targets):,} channels."
                    )

            await flush_batch()

        return processed, inserted


epg_ingest_manager = EPGIngestManager()
