"""event.comment: unbounded text (devices write comments longer than 255 characters)

Revision ID: c4e8a2d6b9f1
Revises: b3d9f1a7c2e4
Create Date: 2026-11-23 19:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8a2d6b9f1'
down_revision = 'b3d9f1a7c2e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.alter_column('comment', existing_type=sa.String(length=255), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.alter_column('comment', existing_type=sa.Text(), type_=sa.String(length=255), existing_nullable=True)
