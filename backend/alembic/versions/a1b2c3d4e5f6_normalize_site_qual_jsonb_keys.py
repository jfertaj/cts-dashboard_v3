"""Normalize site_qual JSONB keys: dots to underscores in section subcode

Revision ID: a1b2c3d4e5f6
Revises: 63d7b6d09a47
Create Date: 2026-02-24

Background
----------
Qual data is stored in site_qual.data (JSONB) with keys like:
  "3_2__personal_conversation_with_physician"  <- canonical (underscore subcode)
  "3.2__personal_conversation_with_physician"  <- legacy (dot subcode)

The backend uses underscore format as canonical (matches the frontend field
catalog). This migration converts all legacy dot-subcode keys to underscores
so the exact-key lookup in _qual_get always succeeds.

Keys without a section prefix are left untouched — _qual_get handles them
via its third fallback.

Idempotent: already-correct keys are unchanged.
"""
import json
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '20251030_qual_comments_tsv'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches a section subcode that contains a dot, e.g. "3.2__"
_DOT_SUBCODE = re.compile(r'^\d+\.\d+__')


def _normalize(data: dict) -> tuple:
    """Convert dot-subcode keys to underscore-subcode keys.
    Returns (new_dict, changed_bool).
    """
    new_data = {}
    changed = False
    for k, v in data.items():
        if _DOT_SUBCODE.match(k):
            new_key = k.replace(".", "_", 1)
            new_data[new_key] = v
            changed = True
        else:
            new_data[k] = v
    return new_data, changed


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, data FROM site_qual")).fetchall()
    updated = 0
    for row_id, data in rows:
        if not data:
            continue
        new_data, changed = _normalize(data)
        if changed:
            conn.execute(
                sa.text(
                    "UPDATE site_qual SET data = :data::jsonb WHERE id = :id"
                ),
                {"data": json.dumps(new_data), "id": row_id},
            )
            updated += 1
    print(f"[normalize_site_qual_jsonb_keys] {updated}/{len(rows)} rows updated.")


def downgrade() -> None:
    # Not reversible without a backup — underscore→dot is ambiguous.
    pass
