"""
Add generated tsvector for qualification comments and GIN index

Revision ID: 20251030_qual_comments_tsv
Revises: 
Create Date: 2025-10-30
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20251030_qual_comments_tsv'
down_revision = '63d7b6d09a47'
branch_labels = None
depends_on = None


def upgrade():
    # 1) helper function to concatenate comment-like fields from JSONB
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.qual_concat_comments(js jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        AS $$
          SELECT coalesce(string_agg(v, ' '), '')
          FROM (
            SELECT value AS v
            FROM jsonb_each_text(js)
            WHERE lower(key) LIKE '%comment%'
          ) t;
        $$;
        """
    )
    # 2) generated column (stored)
    op.execute(
        """
        ALTER TABLE public.site_qual
        ADD COLUMN IF NOT EXISTS comments_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', public.qual_concat_comments(data))) STORED;
        """
    )
    # 3) GIN index
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_site_qual_comments_tsv
        ON public.site_qual USING GIN (comments_tsv);
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_site_qual_comments_tsv;")
    op.execute("ALTER TABLE public.site_qual DROP COLUMN IF EXISTS comments_tsv;")
    op.execute("DROP FUNCTION IF EXISTS public.qual_concat_comments(jsonb);")