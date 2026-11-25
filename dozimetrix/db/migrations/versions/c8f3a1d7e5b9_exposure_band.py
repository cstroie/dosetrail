"""exposure_band: stored anatomy, projection and size bands of radiography exposures

Revision ID: c8f3a1d7e5b9
Revises: b9e3d5a7c1f4
Create Date: 2026-11-25 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8f3a1d7e5b9'
down_revision = 'b9e3d5a7c1f4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'exposure_band',
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('study_id', sa.Integer(), nullable=False),
        sa.Column('exam_code_id', sa.Integer(), nullable=True),
        sa.Column('projection', sa.String(length=8), nullable=True),
        sa.Column('name_kind', sa.String(length=6), nullable=True),
        sa.Column('name_lo', sa.Float(), nullable=True),
        sa.Column('name_hi', sa.Float(), nullable=True),
        sa.Column('name_label', sa.String(length=24), nullable=True),
        sa.Column('age_eu', sa.String(length=12), nullable=True),
        sa.Column('weight_eu', sa.String(length=12), nullable=True),
        sa.Column('age_nat', sa.String(length=12), nullable=True),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['event_id'], ['event.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['study_id'], ['study.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['exam_code_id'], ['code.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('event_id'),
    )
    with op.batch_alter_table('exposure_band', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exposure_band_study_id'), ['study_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exposure_band_age_eu'), ['age_eu'], unique=False)
        batch_op.create_index(batch_op.f('ix_exposure_band_weight_eu'), ['weight_eu'], unique=False)


def downgrade() -> None:
    op.drop_table('exposure_band')
