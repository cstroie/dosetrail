"""event.ssde_calc / ssde_method: SSDE calculated from the water-equivalent diameter

Revision ID: 9b4e2d6f1a70
Revises: 4a7aef6f7f30
Create Date: 2026-11-22 13:30:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = '9b4e2d6f1a70'
down_revision = '4a7aef6f7f30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ssde_calc', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('ssde_method', sa.String(length=24).with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=24), 'mariadb').with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=24), 'mysql'), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_column('ssde_method')
        batch_op.drop_column('ssde_calc')
