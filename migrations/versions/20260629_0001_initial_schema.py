"""initial schema

Revision ID: 20260629_0001
Revises:
Create Date: 2026-06-29 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260629_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

video_status_enum = sa.Enum("AVAILABLE", "DOWNLOADED", "DELETED_ON_YT", name="videostatus")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("storage_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("playlist_quota", sa.Integer(), nullable=False),
        sa.Column("storage_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "videos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("yt_video_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("channel_name", sa.String(length=255), nullable=False),
        sa.Column("duration", sa.Integer(), nullable=False),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("status", video_status_enum, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_videos_yt_video_id", "videos", ["yt_video_id"], unique=True)

    op.create_table(
        "playlists",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("yt_playlist_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("auto_sync", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "yt_playlist_id", name="uq_owner_playlist"),
    )
    op.create_index("ix_playlists_owner_id", "playlists", ["owner_id"], unique=False)
    op.create_index("ix_playlists_yt_playlist_id", "playlists", ["yt_playlist_id"], unique=False)

    op.create_table(
        "playlist_video",
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("local_file_path", sa.Text(), nullable=True),
        sa.Column("format_saved", sa.String(length=16), nullable=True),
        sa.Column("download_error", sa.Text(), nullable=True),
        sa.Column("last_download_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["playlist_id"], ["playlists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("playlist_id", "video_id"),
    )


def downgrade() -> None:
    op.drop_table("playlist_video")
    op.drop_index("ix_playlists_yt_playlist_id", table_name="playlists")
    op.drop_index("ix_playlists_owner_id", table_name="playlists")
    op.drop_table("playlists")
    op.drop_index("ix_videos_yt_video_id", table_name="videos")
    op.drop_table("videos")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    video_status_enum.drop(op.get_bind(), checkfirst=True)
