"""ct_kfactor: DLP to effective dose coefficients entered by the physicist

Revision ID: d9a1c3e5b7f8
Revises: c7f9b1d3e5a6
Create Date: 2026-11-26 14:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9a1c3e5b7f8'
down_revision = 'c7f9b1d3e5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ct_kfactor',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('region', sa.String(16), nullable=False),
        sa.Column('age_to_years', sa.Float()),
        sa.Column('phantom_cm', sa.SmallInteger(), nullable=False),
        sa.Column('k', sa.Float(), nullable=False),
        sa.Column('source', sa.String(255), nullable=False),
        sa.UniqueConstraint('region', 'age_to_years', 'phantom_cm', name='uq_ct_kfactor'),
    )


def downgrade() -> None:
    op.drop_table('ct_kfactor')
