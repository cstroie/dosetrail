"""alert.notified_channels: per-channel delivery state, so a failed channel is retried alone

Revision ID: b3d9f1a7c2e4
Revises: a7c3e9b1d5f2
Create Date: 2026-11-23 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'b3d9f1a7c2e4'
down_revision = 'a7c3e9b1d5f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alert', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notified_channels', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('alert', schema=None) as batch_op:
        batch_op.drop_column('notified_channels')
