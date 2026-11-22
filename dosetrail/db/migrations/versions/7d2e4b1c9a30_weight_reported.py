"""study.weight_reported_kg: the reported weight kept when replaced by an estimate

Revision ID: 7d2e4b1c9a30
Revises: 0c7cea44f04b
Create Date: 2026-11-22 09:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '7d2e4b1c9a30'
down_revision = '0c7cea44f04b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weight_reported_kg', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.drop_column('weight_reported_kg')
