"""auth hardening

Revision ID: 20260630_0002
Revises: 20260629_0001
Create Date: 2026-06-30 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260630_0002"
down_revision: str | None = "20260629_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_tokens_purpose", "auth_tokens", ["purpose"], unique=False)
    op.create_index("ix_auth_tokens_token_hash", "auth_tokens", ["token_hash"], unique=True)
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_user_id", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_token_hash", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_purpose", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
