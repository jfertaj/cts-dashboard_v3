"""add geonames_cities table

Revision ID: 9e4ac666b53b
Revises: 0f75cf09070a
Create Date: 2025-04-12 16:39:05.630674

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e4ac666b53b'
down_revision: Union[str, None] = '0f75cf09070a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        'geonames_cities',
        sa.Column('geonameid', sa.Integer, primary_key=True),
        sa.Column('name', sa.String, nullable=False),
        sa.Column('asciiname', sa.String),
        sa.Column('latitude', sa.Float),
        sa.Column('longitude', sa.Float),
        sa.Column('country_code', sa.String),
        sa.Column('admin1_code', sa.String),  # State/province
        sa.Column('population', sa.BigInteger)
    )

def downgrade():
    op.drop_table('geonames_cities')