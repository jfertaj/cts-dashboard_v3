"""add app_sessions table"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("oid", sa.String(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_app_sessions_expires_at", "app_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_app_sessions_expires_at", table_name="app_sessions")
    op.drop_table("app_sessions")
