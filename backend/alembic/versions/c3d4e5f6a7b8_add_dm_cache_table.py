"""Add dm_cache table for shared Distance Matrix results

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a1
Create Date: 2026-04-21

Background
----------
Distance Matrix results used to live in a process-local dict (_dm_cache in
salesforce_explorer.py). With 4 uvicorn workers, the same origin/destination
pair could be fetched up to 4 times before all workers warmed up, inflating
Google Maps billing. This table is the shared backing store that replaces
the in-memory dict. Keys preserve the existing format
(`dm_km:{origin}->{md5(destinations)}`).

The CREATE TABLE uses IF NOT EXISTS — safe against an already-bootstrapped DB.
"""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS dm_cache (
            key          TEXT PRIMARY KEY,
            value        JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            ttl_seconds  INTEGER NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_dm_cache_created_at
        ON dm_cache (created_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dm_cache_created_at")
    op.drop_table("dm_cache")
