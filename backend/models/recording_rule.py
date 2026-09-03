import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class RecordingRule(Base):
    __tablename__ = "recording_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xtream_accounts.id"), index=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )

    channel_id: Mapped[str] = mapped_column(String(100))
    channel_name: Mapped[str] = mapped_column(String(255))
    title_match: Mapped[str] = mapped_column(String(500))
    match_mode: Mapped[str] = mapped_column(String(32), default="exact")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # JSON array using Python's weekday convention (Monday=0, Sunday=6).
    days_of_week: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Null means recordings are retained indefinitely. When set, only completed
    # downloads created by this rule are eligible for age-based cleanup.
    delete_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    pre_padding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    post_padding_minutes: Mapped[int] = mapped_column(Integer, default=0)
    request_source: Mapped[str] = mapped_column(String(32), default="admin_local")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def parsed_days_of_week(self) -> list[int] | None:
        if not self.days_of_week:
            return None
        try:
            values = json.loads(self.days_of_week)
        except (TypeError, ValueError):
            return None
        return [int(value) for value in values]

    def set_days_of_week(self, values: list[int] | None) -> None:
        self.days_of_week = (
            json.dumps(sorted(set(int(value) for value in values)))
            if values
            else None
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "requested_by_user_id": self.requested_by_user_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "title_match": self.title_match,
            "match_mode": self.match_mode or "exact",
            "enabled": bool(self.enabled),
            "days_of_week": self.parsed_days_of_week,
            "delete_after_days": self.delete_after_days,
            "pre_padding_minutes": self.pre_padding_minutes,
            "post_padding_minutes": self.post_padding_minutes,
            "request_source": self.request_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
