"""Recorded breaks of a goal, so a paused week stays neutral afterwards.

Purely additive. The table starts empty: breaks taken before this revision have
no row and none is invented — those weeks are scored from the quest history
alone, exactly as they were before.

Uniqueness of the open interval is enforced by a *partial* unique index
(``WHERE ended_at IS NULL``) rather than a plain unique constraint, because a
goal may be paused many times and only the still-running break must be unique.
SQLite has supported partial indexes since 3.8.0 (2013), so this works on the
production file as well as on the in-memory test database.

Revision ID: 0004_goal_pause_intervals
Revises: 0003_quest_completions
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004_goal_pause_intervals'
down_revision: Union[str, None] = '0003_quest_completions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'goal_pause_intervals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('goal_id', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_goal_pause_intervals_goal_id'),
        'goal_pause_intervals',
        ['goal_id'],
        unique=False,
    )
    op.create_index(
        'ux_goal_pause_open',
        'goal_pause_intervals',
        ['goal_id'],
        unique=True,
        sqlite_where=sa.text('ended_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ux_goal_pause_open', table_name='goal_pause_intervals')
    op.drop_index(
        op.f('ix_goal_pause_intervals_goal_id'), table_name='goal_pause_intervals'
    )
    op.drop_table('goal_pause_intervals')
