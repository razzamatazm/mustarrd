from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class StarredChannel(Base):
    """A channel a user starred so it floats to the top of the Browse list."""

    __tablename__ = "starred_channels"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "channel_id",
            name="ux_starred_channels_user_account_channel",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("xtream_accounts.id"), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "account_id": self.account_id,
            "channel_id": self.channel_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
