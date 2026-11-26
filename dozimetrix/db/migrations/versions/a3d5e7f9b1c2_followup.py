"""followup: patients with a substantial fluoroscopy dose, to inform and check

Revision ID: a3d5e7f9b1c2
Revises: f1b7c3d9a2e4
Create Date: 2026-11-26 11:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3d5e7f9b1c2'
down_revision = 'f1b7c3d9a2e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'followup',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('study_id', sa.Integer(), sa.ForeignKey('study.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('study_datetime', sa.DateTime(), nullable=True),
        sa.Column('levels', sa.String(255), nullable=False),
        sa.Column('status', sa.String(12), nullable=False, server_default='open'),
        sa.Column('informed_at', sa.DateTime(), nullable=True),
        sa.Column('informed_by', sa.String(64), nullable=True),
        sa.Column('check_due', sa.Date(), nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('outcome', sa.String(255), nullable=True),
        sa.Column('note', sa.String(1024), nullable=True),
        sa.Column('updated_by', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_followup_study_datetime', 'followup', ['study_datetime'])
    op.create_index('ix_followup_status', 'followup', ['status', 'study_datetime'])


def downgrade() -> None:
    op.drop_table('followup')
