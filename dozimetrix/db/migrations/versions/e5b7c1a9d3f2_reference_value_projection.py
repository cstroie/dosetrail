"""reference_value.projection: local radiography DRLs per view (AP/PA, LL)

Revision ID: e5b7c1a9d3f2
Revises: d7a1f3c9e5b2
Create Date: 2026-11-24 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5b7c1a9d3f2'
down_revision = 'd7a1f3c9e5b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('reference_value', schema=None) as batch_op:
        batch_op.add_column(sa.Column('projection', sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('reference_value', schema=None) as batch_op:
        batch_op.drop_column('projection')
