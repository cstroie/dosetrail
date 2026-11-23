"""ESAK: event.esak_calc / esak_method and the device_output curve table (Order 1255/2016 annex 2)

Revision ID: e1a5c7d3b904
Revises: d8f2b6a4c1e9
Create Date: 2026-11-23 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'e1a5c7d3b904'
down_revision = 'd8f2b6a4c1e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'device_output',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('device_id', sa.Integer(), nullable=False),
        sa.Column('kvp', sa.Float(), nullable=False),
        sa.Column('mgy_per_mas', sa.Float(), nullable=False),
        sa.Column('fdd_cm', sa.Float(), server_default='50', nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['device.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'kvp', name='uq_device_output_kvp'),
    )
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('esak_calc', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('esak_method', sa.String(length=16).with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=16), 'mariadb').with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=16), 'mysql'), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_column('esak_method')
        batch_op.drop_column('esak_calc')
    op.drop_table('device_output')
