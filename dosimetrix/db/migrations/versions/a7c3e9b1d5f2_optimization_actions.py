"""optimization_action: the quarterly plan of measures (Order 1255/2016 art. 7(2))

Revision ID: a7c3e9b1d5f2
Revises: f4b8d2a6c1e7
Create Date: 2026-11-23 16:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'a7c3e9b1d5f2'
down_revision = 'f4b8d2a6c1e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'optimization_action',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('year', sa.SmallInteger(), nullable=False),
        sa.Column('quarter', sa.SmallInteger(), nullable=False),
        sa.Column('finding', sa.String(length=1000), nullable=False),
        sa.Column('action', sa.String(length=1000), nullable=False),
        sa.Column('owner', sa.String(length=120), nullable=True),
        sa.Column('status', sa.String(length=8).with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=8), 'mariadb').with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=8), 'mysql'), server_default='open', nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_action_period', 'optimization_action', ['year', 'quarter'])


def downgrade() -> None:
    op.drop_index('ix_action_period', table_name='optimization_action')
    op.drop_table('optimization_action')
