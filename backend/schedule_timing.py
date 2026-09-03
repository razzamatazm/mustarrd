DEFAULT_SCHEDULED_DOWNLOAD_DELAY_MINUTES = 5
MAX_SCHEDULED_DOWNLOAD_DELAY_MINUTES = 120


def resolve_scheduled_download_delay_minutes(value) -> int:
    """Return a safe delay for scheduler and API timing calculations."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULED_DOWNLOAD_DELAY_MINUTES
    if not 0 <= minutes <= MAX_SCHEDULED_DOWNLOAD_DELAY_MINUTES:
        return DEFAULT_SCHEDULED_DOWNLOAD_DELAY_MINUTES
    return minutes


async def get_scheduled_download_delay_minutes(session) -> int:
    """Load the current global delay without coupling models to API modules."""
    from sqlalchemy import select

    from models.settings import AppSettings

    result = await session.execute(select(AppSettings))
    settings = result.scalar_one_or_none()
    return resolve_scheduled_download_delay_minutes(
        settings.scheduled_download_delay_minutes if settings else None
    )
