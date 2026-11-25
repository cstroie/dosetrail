"""shared_view: workspaces and study queries an admin publishes for everyone

Revision ID: b9e3d5a7c1f4
Revises: e5b7c1a9d3f2
Create Date: 2026-11-25 10:00:00
"""
from alembic import op
import sqlalchemy as sa

from dozimetrix.db.types import AsciiId, JSONType, Text

revision = 'b9e3d5a7c1f4'
down_revision = 'e5b7c1a9d3f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'shared_view',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', AsciiId(32), nullable=False),
        sa.Column('name', Text(120), nullable=False),
        sa.Column('value', JSONType(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['app_user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by'], ['app_user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'name', name='uq_shared_view'),
    )


def downgrade() -> None:
    op.drop_table('shared_view')
