"""Optional weekday planning for habits.

Purely additive. The table starts empty, so every existing habit stays exactly
as it was: no plan is invented for it, and a habit without rows here remains
valid and unscheduled ("flexibel"). No habit column changes and no completion
row is touched.

The foreign key to habits deliberately carries no ON DELETE CASCADE: habits are
archived rather than deleted (see app/habits.archive_habit), and historical
completions must never disappear as a side effect of a schedule change.

Revision ID: 0005_habit_schedule_days
Revises: 0004_goal_pause_intervals
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005_habit_schedule_days'
down_revision: Union[str, None] = '0004_goal_pause_intervals'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'habit_schedule_days',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('habit_id', sa.Integer(), nullable=False),
        # ISO weekday: 1 = Monday … 7 = Sunday
        sa.Column('iso_weekday', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['habit_id'], ['habits.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('habit_id', 'iso_weekday', name='ux_habit_schedule_day'),
    )
    op.create_index(
        op.f('ix_habit_schedule_days_habit_id'),
        'habit_schedule_days',
        ['habit_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_habit_schedule_days_habit_id'), table_name='habit_schedule_days'
    )
    op.drop_table('habit_schedule_days')
