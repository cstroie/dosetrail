"""study.glandularity_pct and the effective dose coefficient table (Order 1255/2016 art. 3)

Revision ID: f4b8d2a6c1e7
Revises: e1a5c7d3b904
Create Date: 2026-11-23 15:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'f4b8d2a6c1e7'
down_revision = 'e1a5c7d3b904'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.add_column(sa.Column('glandularity_pct', sa.Float(), nullable=True))
    op.create_table(
        'effective_dose_coefficient',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nuclide', sa.String(length=32), nullable=False),
        sa.Column('form', sa.String(length=64), nullable=False),
        sa.Column('age_to_years', sa.Float(), nullable=True),
        sa.Column('msv_per_mbq', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nuclide', 'form', 'age_to_years', name='uq_effdose'),
    )


def downgrade() -> None:
    op.drop_table('effective_dose_coefficient')
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.drop_column('glandularity_pct')
