"""index study (device_id, modality): the modalities of a device without reading its studies

The overview asks which modalities the local devices produce and the Devices page counts studies
per device and modality; both scanned every study (0.8 s and 0.3 s at 640k studies). Covering
index, so they read the index only.

Revision ID: d4a7e2c9b6f1
Revises: c8f3a1d7e5b9
Create Date: 2026-11-25 19:00:00
"""
from alembic import op


revision = 'd4a7e2c9b6f1'
down_revision = 'c8f3a1d7e5b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_study_device_mod', 'study', ['device_id', 'modality'])


def downgrade() -> None:
    op.drop_index('ix_study_device_mod', table_name='study')
