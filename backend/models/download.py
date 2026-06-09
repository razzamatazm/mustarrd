from datetime import datetime
from enum import Enum
from sqlalchemy import String, Integer, DateTime, Float, ForeignKey, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class DownloadStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xtream_accounts.id"))

    # Channel info
    channel_id: Mapped[str] = mapped_column(String(100))
    channel_name: Mapped[str] = mapped_column(String(255))

    # Program info
    program_title: Mapped[str] = mapped_column(String(500))
    program_start: Mapped[datetime] = mapped_column(DateTime)
    program_end: Mapped[datetime] = mapped_column(DateTime)
    start_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    stop_timestamp: Mapped[int] = mapped_column(Integer, default=0)
    duration_minutes: Mapped[int] = mapped_column(Integer)

    # Download info
    source_url: Mapped[str] = mapped_column(Text)
    output_path: Mapped[str] = mapped_column(String(1000))
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    request_source: Mapped[str] = mapped_column(String(32), default="admin")
    status: Mapped[str] = mapped_column(String(50), default=DownloadStatus.PENDING.value)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_vod: Mapped[bool] = mapped_column(Boolean, default=False)
    # Actual duration of the recorded file (ffprobe), as opposed to the EPG
    # program duration in duration_minutes. Null when probing was unavailable.
    recorded_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "program_title": self.program_title,
            "program_start": self.program_start.isoformat() if self.program_start else None,
            "program_end": self.program_end.isoformat() if self.program_end else None,
            "start_timestamp": self.start_timestamp,
            "stop_timestamp": self.stop_timestamp,
            "duration_minutes": self.duration_minutes,
            "output_path": self.output_path,
            "requested_by_user_id": self.requested_by_user_id,
            "request_source": self.request_source,
            "status": self.status,
            "progress": self.progress,
            "file_size": self.file_size,
            "downloaded_bytes": self.downloaded_bytes,
            "error_message": self.error_message,
            "is_vod": self.is_vod,
            "recorded_duration_seconds": self.recorded_duration_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
