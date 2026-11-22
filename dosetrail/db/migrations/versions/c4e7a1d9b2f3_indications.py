"""clinical indications: study.indication_text / indication_source, study_indication

Revision ID: c4e7a1d9b2f3
Revises: 9b4e2d6f1a70
Create Date: 2026-11-22 14:30:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = 'c4e7a1d9b2f3'
down_revision = '9b4e2d6f1a70'
branch_labels = None
depends_on = None


def _ascii(n):
    return sa.String(length=n).with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=n), 'mariadb').with_variant(mysql.VARCHAR(charset='ascii', collation='ascii_bin', length=n), 'mysql')


def upgrade() -> None:
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.add_column(sa.Column('indication_text', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('indication_source', _ascii(8), nullable=True))
    op.create_table('study_indication',
    sa.Column('study_id', sa.Integer(), nullable=False),
    sa.Column('code_id', sa.Integer(), nullable=False),
    sa.Column('status', _ascii(12), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('source', _ascii(12), nullable=True),
    sa.Column('matched_text', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['code_id'], ['code.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['study_id'], ['study.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('study_id')
    )
    with op.batch_alter_table('study_indication', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_study_indication_code_id'), ['code_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('study_indication', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_study_indication_code_id'))
    op.drop_table('study_indication')
    with op.batch_alter_table('study', schema=None) as batch_op:
        batch_op.drop_column('indication_source')
        batch_op.drop_column('indication_text')
