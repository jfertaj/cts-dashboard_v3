"""Add sf_sessions table to Alembic migration history

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-03-17

Background
----------
The sf_sessions table has always been created by salesforce_oauth.py via a raw
psycopg3 DDL call (_ensure_sessions_table) at startup, bypassing Alembic.
This migration brings it into the migration history so future ALTER TABLE
statements can reference it safely.

The CREATE TABLE uses IF NOT EXISTS — safe to apply against a production DB
where the table already exists. The downgrade drops it only if explicitly run.
"""

from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a1"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS sf_sessions (
            session_id    TEXT PRIMARY KEY,
            access_token  TEXT NOT NULL,
            instance_url  TEXT NOT NULL,
            issued_at     INTEGER NOT NULL,
            refresh_token TEXT,
            token_type    TEXT DEFAULT 'Bearer',
            id_url        TEXT
        )
    """)


def downgrade() -> None:
    op.drop_table("sf_sessions")
