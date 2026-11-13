from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class Notification(Base):
    """One problem the user should know about, shown under the bell (#430).

    ``key`` makes it one row per problem: ``account_expiry:3``,
    ``account_connection:3``, ``recording_failed:42``. Raising the same key
    again updates the row instead of adding one. A problem that goes away
    (account renewed, connection back, retry succeeded) deletes its row.

    Audience: admins see every row. A download-only user sees only rows whose
    ``user_id`` is theirs, which in practice means their own failed recordings.
    ``account_id`` and ``download_id`` are plain integers, not foreign keys, so
    deleting an account or a download never trips over a notification.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    download_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "link": self.link,
            "account_id": self.account_id,
            "download_id": self.download_id,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class NotificationDismissal(Base):
    """A user hid a notification. Hidden for them only, not for anyone else."""

    __tablename__ = "notification_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", "user_id", name="ux_notification_dismissals_notification_user"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notifications.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    dismissed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
