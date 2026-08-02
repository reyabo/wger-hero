"""Make the optional learning metrics of a Japanese SAVE nullable.

A SAVE need not report a WaniKani level or an SRS grammar-point count. Until
now both columns were NOT NULL with default 0, so "not stated" could only be
recorded by writing a number that was never counted — the one fact the import
must not invent. Making them nullable is what lets NULL mean "not stated" and 0
mean "counted zero".

Existing rows are untouched: every value stays exactly as it was, and dropping
a NOT NULL constraint cannot invalidate data that is already there. No value is
rewritten and no row is deleted.

SQLite cannot ALTER a column in place, so batch_alter_table rebuilds the table —
Alembic copies the rows over unchanged.

The downgrade is the one lossy direction, unavoidably: restoring NOT NULL
requires a value for every row, so rows written after this revision that say
"not stated" become 0. Nothing else changes. Take a backup first, as
docs/DEPLOY.md says for every migration.

Revision ID: 0006_optional_learning_metrics
Revises: 0005_habit_schedule_days
Create Date: 2026-08-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006_optional_learning_metrics'
down_revision: Union[str, None] = '0005_habit_schedule_days'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('japanese_save_imports') as batch:
        batch.alter_column(
            'wanikani_level', existing_type=sa.Integer(), nullable=True
        )
        batch.alter_column(
            'bunpro_points', existing_type=sa.Integer(), nullable=True
        )


def downgrade() -> None:
    # NOT NULL needs a value everywhere. Only rows that say "not stated" are
    # affected; every counted value is left alone.
    op.execute(
        "UPDATE japanese_save_imports SET wanikani_level = 0 "
        "WHERE wanikani_level IS NULL"
    )
    op.execute(
        "UPDATE japanese_save_imports SET bunpro_points = 0 "
        "WHERE bunpro_points IS NULL"
    )
    with op.batch_alter_table('japanese_save_imports') as batch:
        batch.alter_column(
            'wanikani_level', existing_type=sa.Integer(), nullable=False
        )
        batch.alter_column(
            'bunpro_points', existing_type=sa.Integer(), nullable=False
        )
