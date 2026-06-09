from sqlalchemy import String, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from database import Base
from config import settings as app_settings


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    download_folder: Mapped[str] = mapped_column(
        String(1000),
        default=lambda: app_settings.default_download_folder,
    )
    completed_folder: Mapped[str] = mapped_column(
        String(1000),
        default=lambda: app_settings.default_completed_folder,
    )

    # Naming templates
    tv_template: Mapped[str] = mapped_column(
        String(500),
        default="{show} - S{season:02d}E{episode:02d} - {title}"
    )
    movie_template: Mapped[str] = mapped_column(
        String(500),
        default="{title} ({year})"
    )
    sports_template: Mapped[str] = mapped_column(
        String(500),
        default="{title} - {date}"
    )
    default_template: Mapped[str] = mapped_column(
        String(500),
        default="{title} - {date}"
    )

    max_concurrent_downloads: Mapped[int] = mapped_column(Integer, default=2)
    min_free_space_gb: Mapped[int] = mapped_column(Integer, default=25)
    default_pre_padding_minutes: Mapped[int] = mapped_column(Integer, default=1)
    default_post_padding_minutes: Mapped[int] = mapped_column(Integer, default=5)
    default_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_concurrent_post_processing: Mapped[int] = mapped_column(Integer, default=1)

    # Post-processing options
    transcode_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    transcode_format: Mapped[str] = mapped_column(String(10), default="mkv")  # ts, mp4, mkv
    hw_accel: Mapped[str] = mapped_column(String(20), default="cpu")  # cpu, videotoolbox, nvenc, amf, vaapi
    delete_original_after_transcode: Mapped[bool] = mapped_column(Boolean, default=True)
    remux_only: Mapped[bool] = mapped_column(Boolean, default=True)
    epg_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    show_future_programs: Mapped[bool] = mapped_column(Boolean, default=False)
    launch_on_startup: Mapped[bool] = mapped_column(Boolean, default=True)

    # Post-download ffprobe sanity check; flags suspect files as
    # "Completed with warnings" without failing the recording.
    integrity_check_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    comskip_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    comskip_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    comskip_ini_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    admin_password_hash: Mapped[str | None] = mapped_column(String(500), nullable=True)
    admin_username_bootstrap_required: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_processing_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_comskip_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarding_selected_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plex_outbound_policy: Mapped[str] = mapped_column(
        String(64),
        default="resource_connections_only",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "download_folder": self.download_folder,
            "completed_folder": self.completed_folder,
            "tv_template": self.tv_template,
            "movie_template": self.movie_template,
            "sports_template": self.sports_template,
            "default_template": self.default_template,
            "max_concurrent_downloads": self.max_concurrent_downloads,
            "min_free_space_gb": self.min_free_space_gb,
            "default_pre_padding_minutes": self.default_pre_padding_minutes,
            "default_post_padding_minutes": self.default_post_padding_minutes,
            "default_account_id": self.default_account_id,
            "max_concurrent_post_processing": self.max_concurrent_post_processing,
            "transcode_enabled": self.transcode_enabled,
            "transcode_format": self.transcode_format,
            "hw_accel": self.hw_accel,
            "delete_original_after_transcode": self.delete_original_after_transcode,
            "remux_only": self.remux_only,
            "integrity_check_enabled": self.integrity_check_enabled,
            "comskip_enabled": self.comskip_enabled,
            "comskip_path": self.comskip_path,
            "comskip_ini_path": self.comskip_ini_path,
            "admin_username_bootstrap_required": self.admin_username_bootstrap_required,
            "epg_offset_minutes": self.epg_offset_minutes,
            "show_future_programs": self.show_future_programs,
            "launch_on_startup": self.launch_on_startup,
            "onboarding_dismissed": self.onboarding_dismissed,
            "onboarding_processing_confirmed": self.onboarding_processing_confirmed,
            "onboarding_comskip_confirmed": self.onboarding_comskip_confirmed,
            "onboarding_selected_profile": self.onboarding_selected_profile,
            "plex_outbound_policy": self.plex_outbound_policy,
        }
