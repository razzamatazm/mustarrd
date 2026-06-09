import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from config import settings


class Base(DeclarativeBase):
    pass


logger = logging.getLogger(__name__)


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_lightweight_migrations(conn)
    await _migrate_legacy_account_passwords()


async def _column_exists(conn, table_name: str, column_name: str) -> bool:
    def _check(sync_conn):
        inspector = inspect(sync_conn)
        return any(col["name"] == column_name for col in inspector.get_columns(table_name))

    return await conn.run_sync(_check)


async def _apply_lightweight_migrations(conn) -> None:
    # Keep startup-safe schema additions here for existing installations.
    if not await _column_exists(conn, "plex_servers", "connection_uri"):
        await conn.execute(text("ALTER TABLE plex_servers ADD COLUMN connection_uri VARCHAR(1000)"))

    if not await _column_exists(conn, "app_settings", "plex_outbound_policy"):
        await conn.execute(
            text(
                "ALTER TABLE app_settings "
                "ADD COLUMN plex_outbound_policy VARCHAR(64) DEFAULT 'resource_connections_only'"
            )
        )

    if not await _column_exists(conn, "app_settings", "default_account_id"):
        await conn.execute(text("ALTER TABLE app_settings ADD COLUMN default_account_id INTEGER"))

    if not await _column_exists(conn, "epg_programs", "provider_start"):
        await conn.execute(text("ALTER TABLE epg_programs ADD COLUMN provider_start VARCHAR(255)"))

    if not await _column_exists(conn, "epg_programs", "provider_stop"):
        await conn.execute(text("ALTER TABLE epg_programs ADD COLUMN provider_stop VARCHAR(255)"))

    if not await _column_exists(conn, "xtream_accounts", "catchup_resolution_mode"):
        await conn.execute(
            text("ALTER TABLE xtream_accounts ADD COLUMN catchup_resolution_mode VARCHAR(32) DEFAULT 'auto'")
        )

    if not await _column_exists(conn, "xtream_accounts", "catchup_fallback_offset_minutes"):
        await conn.execute(
            text("ALTER TABLE xtream_accounts ADD COLUMN catchup_fallback_offset_minutes INTEGER DEFAULT 0")
        )

    if not await _column_exists(conn, "xtream_accounts", "guide_offset_hours"):
        await conn.execute(
            text("ALTER TABLE xtream_accounts ADD COLUMN guide_offset_hours INTEGER DEFAULT 0")
        )

    if not await _column_exists(conn, "scheduled_recordings", "channel_category_name"):
        await conn.execute(text("ALTER TABLE scheduled_recordings ADD COLUMN channel_category_name VARCHAR(255)"))

    if not await _column_exists(conn, "scheduled_recordings", "provider_start"):
        await conn.execute(text("ALTER TABLE scheduled_recordings ADD COLUMN provider_start VARCHAR(255)"))

    if not await _column_exists(conn, "scheduled_recordings", "provider_stop"):
        await conn.execute(text("ALTER TABLE scheduled_recordings ADD COLUMN provider_stop VARCHAR(255)"))

    if not await _column_exists(conn, "downloads", "start_timestamp"):
        await conn.execute(text("ALTER TABLE downloads ADD COLUMN start_timestamp INTEGER DEFAULT 0"))

    if not await _column_exists(conn, "downloads", "stop_timestamp"):
        await conn.execute(text("ALTER TABLE downloads ADD COLUMN stop_timestamp INTEGER DEFAULT 0"))

    if not await _column_exists(conn, "xtream_accounts", "last_connection_ok"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN last_connection_ok BOOLEAN"))

    if not await _column_exists(conn, "xtream_accounts", "last_connection_checked_at"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN last_connection_checked_at DATETIME"))

    if not await _column_exists(conn, "xtream_accounts", "last_connection_error"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN last_connection_error TEXT"))

    if not await _column_exists(conn, "xtream_accounts", "catchup_channel_count"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN catchup_channel_count INTEGER"))

    if not await _column_exists(conn, "xtream_accounts", "catchup_max_archive_days"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN catchup_max_archive_days INTEGER"))

    if not await _column_exists(conn, "xtream_accounts", "vod_available"):
        await conn.execute(text("ALTER TABLE xtream_accounts ADD COLUMN vod_available BOOLEAN"))

    if not await _column_exists(conn, "downloads", "recorded_duration_seconds"):
        await conn.execute(text("ALTER TABLE downloads ADD COLUMN recorded_duration_seconds INTEGER"))

    if not await _column_exists(conn, "app_settings", "integrity_check_enabled"):
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN integrity_check_enabled BOOLEAN DEFAULT 1")
        )


async def _migrate_legacy_account_passwords() -> None:
    from models import XtreamAccount
    from services.account_credentials import encrypt_account_password

    async with async_session_maker() as session:
        result = await session.execute(select(XtreamAccount).where(XtreamAccount.password != ""))
        accounts = result.scalars().all()

        migrated = 0
        for account in accounts:
            if not account.password or account.password_encrypted:
                continue
            account.password_encrypted = encrypt_account_password(account.password)
            account.password = ""
            migrated += 1

        if migrated:
            await session.commit()
            logger.info("Migrated %s legacy account credentials to encrypted storage", migrated)


async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
