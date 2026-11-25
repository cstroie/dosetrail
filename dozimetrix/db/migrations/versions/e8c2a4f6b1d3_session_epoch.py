"""app_user.session_epoch: raised at sign-out, so a copied session token stops working

Revision ID: e8c2a4f6b1d3
Revises: d4a7e2c9b6f1
Create Date: 2026-11-25 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'e8c2a4f6b1d3'
down_revision = 'd4a7e2c9b6f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column('session_epoch', sa.Integer(), nullable=False,
                                        server_default='0'))


def downgrade() -> None:
    op.drop_column('app_user', 'session_epoch')
