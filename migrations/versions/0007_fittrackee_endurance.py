"""FitTrackee endurance sync: sports, workouts, connection, quest weekdays

Additive only. Every new table is new, the one new column is nullable, and
nothing existing is rewritten — no XP event, no quest, no habit and no
completion is touched. The downgrade drops exactly what the upgrade created.

Revision ID: 0007_fittrackee_endurance
Revises: 0006_optional_learning_metrics
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_fittrackee_endurance"
down_revision: Union[str, None] = "0006_optional_learning_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fittrackee_sports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sport_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("counts_for_endurance", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fittrackee_sports_sport_id", "fittrackee_sports", ["sport_id"], unique=True
    )

    op.create_table(
        "fittrackee_workouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("workout_at", sa.DateTime(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("sport_id", sa.Integer(), nullable=False),
        sa.Column("sport_label", sa.String(length=100), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("moving_seconds", sa.Integer(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("ave_speed", sa.Float(), nullable=True),
        sa.Column("max_speed", sa.Float(), nullable=True),
        sa.Column("ave_hr", sa.Integer(), nullable=True),
        sa.Column("max_hr", sa.Integer(), nullable=True),
        sa.Column("remote_modified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "qualifies_for_endurance", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("reward_eligible", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fittrackee_workouts_external_id",
        "fittrackee_workouts",
        ["external_id"],
        unique=True,
    )
    op.create_index(
        "ix_fittrackee_workouts_workout_at", "fittrackee_workouts", ["workout_at"]
    )
    op.create_index(
        "ix_fittrackee_workouts_sport_id", "fittrackee_workouts", ["sport_id"]
    )
    op.create_index(
        "ix_ft_workouts_qualifying",
        "fittrackee_workouts",
        ["qualifies_for_endurance", "reward_eligible"],
    )
    op.create_index("ix_ft_workouts_local_date", "fittrackee_workouts", ["local_date"])

    op.create_table(
        "fittrackee_connection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("base_url", sa.String(length=300), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("baseline_at", sa.DateTime(), nullable=True),
        sa.Column("baseline_workouts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("sports_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("needs_reauth", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Nullable, so every existing quest keeps its meaning: no restriction.
    with op.batch_alter_table("quests") as batch:
        batch.add_column(sa.Column("allowed_weekdays", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("quests") as batch:
        batch.drop_column("allowed_weekdays")

    op.drop_table("fittrackee_connection")
    op.drop_index("ix_ft_workouts_local_date", table_name="fittrackee_workouts")
    op.drop_index("ix_ft_workouts_qualifying", table_name="fittrackee_workouts")
    op.drop_index("ix_fittrackee_workouts_sport_id", table_name="fittrackee_workouts")
    op.drop_index("ix_fittrackee_workouts_workout_at", table_name="fittrackee_workouts")
    op.drop_index("ix_fittrackee_workouts_external_id", table_name="fittrackee_workouts")
    op.drop_table("fittrackee_workouts")
    op.drop_index("ix_fittrackee_sports_sport_id", table_name="fittrackee_sports")
    op.drop_table("fittrackee_sports")
