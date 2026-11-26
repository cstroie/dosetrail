"""incident, incident_log: investigations of alerts, with an audit trail

Revision ID: b5e7a9c1d3f4
Revises: a3d5e7f9b1c2
Create Date: 2026-11-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = 'b5e7a9c1d3f4'
down_revision = 'a3d5e7f9b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'incident',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('alert_id', sa.Integer(), sa.ForeignKey('alert.id', ondelete='SET NULL')),
        sa.Column('study_id', sa.Integer(), sa.ForeignKey('study.id', ondelete='SET NULL')),
        sa.Column('status', sa.String(12), nullable=False, server_default='open'),
        sa.Column('opened_by', sa.String(64)),
        sa.Column('closed_at', sa.DateTime()),
        sa.Column('cause', sa.Text()),
        sa.Column('actions', sa.Text()),
        sa.Column('outcome', sa.Text()),
        sa.Column('reportable', sa.Boolean()),
        sa.Column('reportable_basis', sa.String(1024)),
        sa.Column('reported_at', sa.DateTime()),
        sa.Column('report_reference', sa.String(128)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_incident_alert_id', 'incident', ['alert_id'])
    op.create_index('ix_incident_study_id', 'incident', ['study_id'])
    op.create_table(
        'incident_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('incident_id', sa.Integer(), sa.ForeignKey('incident.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('username', sa.String(64)),
        sa.Column('kind', sa.String(12), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
    )
    op.create_index('ix_incident_log_incident_id', 'incident_log', ['incident_id'])


def downgrade() -> None:
    op.drop_table('incident_log')
    op.drop_table('incident')
