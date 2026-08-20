"""Japanese SAVE version 2: cumulative XP, rank bosses and their rewards

Additive only. Existing rows keep every value they have and are stamped
save_version = 1, which is exactly what they are: snapshots written under the
level-internal schema. No historical import is reinterpreted, no XP event is
touched, and no reward is granted retroactively.

Revision ID: 0008_japanese_save_version_2
Revises: 0007_fittrackee_endurance
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_japanese_save_version_2"
down_revision: Union[str, None] = "0007_fittrackee_endurance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("japanese_save_imports") as batch:
        # Every row that already exists was written under version 1. The
        # server default states that for existing rows; the Python-side default
        # keeps new inserts explicit.
        batch.add_column(
            sa.Column(
                "save_version", sa.Integer(), nullable=False, server_default=sa.text("1")
            )
        )
        batch.add_column(sa.Column("rank_boss_id", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("rank_boss_status", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("rank_reward_id", sa.String(length=50), nullable=True))

    op.create_table(
        "japanese_rank_rewards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("boss_id", sa.String(length=50), nullable=False),
        sa.Column("reward_id", sa.String(length=50), nullable=False),
        sa.Column("reward_name", sa.String(length=100), nullable=False),
        sa.Column("boss_title", sa.String(length=200), nullable=False),
        sa.Column("source_level", sa.Integer(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("unlocked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The unique index is the idempotency guarantee: a second grant for the
    # same boss cannot be inserted even if two requests race.
    op.create_index(
        "ix_japanese_rank_rewards_boss_id",
        "japanese_rank_rewards",
        ["boss_id"],
        unique=True,
    )
    op.create_index(
        "ix_japanese_rank_rewards_import_id", "japanese_rank_rewards", ["import_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_japanese_rank_rewards_import_id", table_name="japanese_rank_rewards")
    op.drop_index("ix_japanese_rank_rewards_boss_id", table_name="japanese_rank_rewards")
    op.drop_table("japanese_rank_rewards")

    with op.batch_alter_table("japanese_save_imports") as batch:
        batch.drop_column("rank_reward_id")
        batch.drop_column("rank_boss_status")
        batch.drop_column("rank_boss_id")
        batch.drop_column("save_version")
