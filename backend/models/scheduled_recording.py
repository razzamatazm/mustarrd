from datetime import datetime, timezone, timedelta
from enum import Enum
from sqlalchemy import String, Integer, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class ScheduledStatus(str, Enum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED_LOW_SPACE = "paused_low_space"


class ScheduledRecording(Base):
    __tablename__ = "scheduled_recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xtream_accounts.id"))

    # Channel info
    channel_id: Mapped[str] = mapped_column(String(100))
    channel_name: Mapped[str] = mapped_column(String(255))
    channel_category_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Program info
    program_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    epg_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    program_title: Mapped[str] = mapped_column(String(500))
    program_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    program_start: Mapped[datetime] = mapped_column(DateTime)
    program_end: Mapped[datetime] = mapped_column(DateTime)
    start_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    stop_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    provider_start: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_stop: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer)

    # Schedule info
    pre_padding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    post_padding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    custom_filename: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    status: Mapped[str] = mapped_column(String(50), default=ScheduledStatus.SCHEDULED.value)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    request_source: Mapped[str] = mapped_column(String(32), default="admin")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def available_at_utc(self) -> datetime | None:
        if self.stop_timestamp:
            return datetime.fromtimestamp(int(self.stop_timestamp), tz=timezone.utc) + timedelta(
                minutes=int(self.post_padding_minutes or 0)
            )
        if self.program_end:
            end_time = self.program_end
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            return end_time + timedelta(minutes=int(self.post_padding_minutes or 0))
        return None

    def to_dict(self):
        available_at = self.available_at_utc()
        return {
            "id": self.id,
            "account_id": self.account_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "program_id": self.program_id,
            "epg_id": self.epg_id,
            "program_title": self.program_title,
            "program_description": self.program_description,
            "program_start": self.program_start.isoformat() if self.program_start else None,
            "program_end": self.program_end.isoformat() if self.program_end else None,
            "start_timestamp": self.start_timestamp,
            "stop_timestamp": self.stop_timestamp,
            "provider_start": self.provider_start,
            "provider_stop": self.provider_stop,
            "duration_minutes": self.duration_minutes,
            "pre_padding_minutes": self.pre_padding_minutes,
            "post_padding_minutes": self.post_padding_minutes,
            "custom_filename": self.custom_filename,
            "status": self.status,
            "status_message": self.status_message,
            "download_id": self.download_id,
            "requested_by_user_id": self.requested_by_user_id,
            "request_source": self.request_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "available_at": available_at.isoformat() if available_at else None,
        }
