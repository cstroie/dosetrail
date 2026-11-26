"""event.dose_check_notification: a CT Dose Check notification was raised

Revision ID: f1b7c3d9a2e4
Revises: e8c2a4f6b1d3
Create Date: 2026-11-26 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1b7c3d9a2e4'
down_revision = 'e8c2a4f6b1d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('event', sa.Column('dose_check_notification', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('event', 'dose_check_notification')
