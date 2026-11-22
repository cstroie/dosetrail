"""patient.demo_source / demo_conflict: provenance of sex and birth date (CNP > HIS > DICOM)

Revision ID: 5f1c2a7e9b10
Revises: 7d2e4b1c9a30
Create Date: 2026-11-22 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

ASCII = dict(charset='ascii', collation='ascii_bin')


revision = '5f1c2a7e9b10'
down_revision = '7d2e4b1c9a30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('patient', schema=None) as batch_op:
        batch_op.add_column(sa.Column('demo_source', sa.String(length=8).with_variant(mysql.VARCHAR(length=8, **ASCII), 'mariadb').with_variant(mysql.VARCHAR(length=8, **ASCII), 'mysql'), nullable=True))
        batch_op.add_column(sa.Column('demo_conflict', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('patient', schema=None) as batch_op:
        batch_op.drop_column('demo_conflict')
        batch_op.drop_column('demo_source')
