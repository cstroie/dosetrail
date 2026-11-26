"""ct_centering: the patient's position against the CT isocentre, from one axial image

Revision ID: e2b4d6f8a0c1
Revises: d9a1c3e5b7f8
Create Date: 2026-11-26 15:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2b4d6f8a0c1'
down_revision = 'd9a1c3e5b7f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ct_centering',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('study_id', sa.Integer(), sa.ForeignKey('study.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('series_uid', sa.String(64)),
        sa.Column('measured', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.String(255)),
        sa.Column('offset_x_mm', sa.Float()),
        sa.Column('offset_y_mm', sa.Float()),
        sa.Column('body_w_mm', sa.Float()),
        sa.Column('body_h_mm', sa.Float()),
        sa.Column('method', sa.String(24)),
        sa.Column('measured_at', sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('ct_centering')
