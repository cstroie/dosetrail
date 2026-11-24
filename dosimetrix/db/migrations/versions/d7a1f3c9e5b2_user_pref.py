"""user_pref: a user's settings kept on the server (chart workspaces)

Revision ID: d7a1f3c9e5b2
Revises: c4e8a2d6b9f1
Create Date: 2026-11-24 10:00:00
"""
from alembic import op
import sqlalchemy as sa

from dosimetrix.db.types import AsciiId, JSONType

revision = 'd7a1f3c9e5b2'
down_revision = 'c4e8a2d6b9f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_pref',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key', AsciiId(128), nullable=False),
        sa.Column('value', JSONType(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'key', name='uq_user_pref'),
    )


def downgrade() -> None:
    op.drop_table('user_pref')
