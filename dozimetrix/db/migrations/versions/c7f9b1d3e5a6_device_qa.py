"""device_qa: QA and calibration register per device

Revision ID: c7f9b1d3e5a6
Revises: b5e7a9c1d3f4
Create Date: 2026-11-26 13:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7f9b1d3e5a6'
down_revision = 'b5e7a9c1d3f4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'device_qa',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('device_id', sa.Integer(), sa.ForeignKey('device.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('done_on', sa.Date(), nullable=False),
        sa.Column('next_due', sa.Date()),
        sa.Column('factor', sa.Float()),
        sa.Column('result', sa.String(255)),
        sa.Column('note', sa.String(1024)),
        sa.Column('entered_by', sa.String(64)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_device_qa_device_id', 'device_qa', ['device_id'])


def downgrade() -> None:
    op.drop_table('device_qa')
