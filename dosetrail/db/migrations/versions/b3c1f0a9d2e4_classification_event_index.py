"""index classification by event (exposure-level exam codes)

Revision ID: b3c1f0a9d2e4
Revises: ac788865e186
Create Date: 2026-11-21 19:00:00
"""
from alembic import op

revision = 'b3c1f0a9d2e4'
down_revision = 'ac788865e186'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_classification_event', 'classification', ['event_id'])


def downgrade() -> None:
    op.drop_index('ix_classification_event', table_name='classification')
