from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class HeroProfile(Base):
    __tablename__ = "hero_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="Hero")
    level: Mapped[int] = mapped_column(Integer, default=1)
    total_xp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), default="wger")
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    raw_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class XpEvent(Base):
    __tablename__ = "xp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50), default="wger")
    source_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    xp: Mapped[int] = mapped_column(Integer)
    attribute: Mapped[str] = mapped_column(String(50), default="Strength")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Quest(Base):
    __tablename__ = "quests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # quest_type: manual | habit_count | workout_count (legacy seeds: weekly)
    quest_type: Mapped[str] = mapped_column(String(50), default="weekly")
    # period: daily | weekly | monthly | once
    period: Mapped[str] = mapped_column(String(20), default="weekly")
    target_value: Mapped[int] = mapped_column(Integer, default=1)
    current_value: Mapped[int] = mapped_column(Integer, default=0)
    # Stable link to one habit for habit_count quests. Takes precedence over
    # match_text, which stays as a fallback for quests created before this
    # existed. A renamed or archived habit keeps counting correctly.
    habit_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Legacy fallback: substring matched against habit titles for habit_count
    # quests, and the comma-separated term list for workout_variety.
    match_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    xp_reward: Mapped[int] = mapped_column(Integer, default=100)
    # JSON object of {stat_key: xp} awarded on completion
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attribute: Mapped[str] = mapped_column(String(50), default="Strength")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When true, the quest re-arms for the next period after completion
    repeatable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Reward calculation fields
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    duration_size: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    effort: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Optional link to a Goal. Quests without a goal keep working unchanged.
    goal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # A one-off, goal-linked quest is displayed as a milestone of that goal.
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional weekday restriction for sources that count dated activities, as
    # a sorted list of ISO weekday digits ("2", "6,7"). NULL means "any day",
    # which is how every quest before this behaved. Validated on write by
    # quests.serialize_allowed_weekdays — never free-form data.
    allowed_weekdays: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Python-side defaults (not server_default) so values are always supplied on
    # insert, even on databases migrated via ALTER TABLE ADD COLUMN.
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True
    )


class ApiCheckEvent(Base):
    __tablename__ = "api_check_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    endpoint: Mapped[str] = mapped_column(String(200))
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    is_success: Mapped[bool] = mapped_column(Boolean, default=False)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unlocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Habit(Base):
    """A repeatable, user-defined action that can be completed for XP."""

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # recurrence: daily | weekly | monthly | flexible
    recurrence: Mapped[str] = mapped_column(String(20), default="daily")
    target_count: Mapped[int] = mapped_column(Integer, default=1)
    base_xp_reward: Mapped[int] = mapped_column(Integer, default=20)
    # JSON object of {stat_key: xp} awarded on each completion
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Reward calculation fields (category/duration/effort → auto-calculated XP)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    duration_size: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    effort: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Optional link to a Goal. Habits without a goal keep working unchanged.
    goal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class HabitScheduleDay(Base):
    """One weekday a habit is planned for, as an ISO weekday (1 = Mon … 7 = Sun).

    Optional by design: a habit with no rows here is not tied to any weekday and
    keeps working exactly as before — it simply shows up as "flexibel" rather
    than being planned for a given day. Numbers rather than German labels are
    stored so the data stays language-independent and validatable.

    A missed planned day is never a failure: nothing here awards or removes XP,
    and no completion is ever created from a plan.
    """

    __tablename__ = "habit_schedule_days"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    habit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("habits.id"), index=True, nullable=False
    )
    iso_weekday: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("habit_id", "iso_weekday", name="ux_habit_schedule_day"),
    )


class HabitCompletion(Base):
    """One recorded completion of a habit (the auditable source of habit XP)."""

    __tablename__ = "habit_completions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    habit_id: Mapped[int] = mapped_column(Integer, index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    stat_xp_awarded: Mapped[int] = mapped_column(Integer, default=0)


class Goal(Base):
    """A long-running personal goal that habits and quests can belong to.

    A goal is never deleted through the UI — it is archived. Pausing is a
    first-class state so a break is recorded as a deliberate choice rather than
    showing up as a gap or a failure anywhere.
    """

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Neutral short label shown on compact cards, e.g. "Training".
    short_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # active | paused | completed | archived
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class GoalPauseInterval(Base):
    """One deliberate break of a goal, from the moment it was paused.

    Momentum and streaks must know whether a week was paused *at the time*,
    not whether the goal happens to be paused now. Without this record the only
    available answer would be the current status applied backwards, which turns
    old breaks into failures the moment a goal is resumed.

    Append-only in the same sense as QuestCompletion: rows are created when a
    goal is paused and are only ever changed by closing the open interval when
    the goal leaves the paused state. There is no edit or delete route, and no
    interval is invented for breaks that happened before this table existed.
    """

    __tablename__ = "goal_pause_intervals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_id: Mapped[int] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # NULL means the goal is still paused — at most one such row per goal,
    # enforced by the partial unique index below rather than by Python alone.
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "ux_goal_pause_open",
            "goal_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
        ),
    )


class JapaneseSaveImport(Base):
    """An imported Japanese coach SAVE snapshot (append-only, never edited).

    Stores the source progress exactly as reported plus the derived global XP
    that was awarded for it. The five language scores are kept as absolute
    snapshot values and are deliberately NOT mapped onto the ten HeroStat
    attributes — no transparent mapping has been agreed yet.
    """

    __tablename__ = "japanese_save_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    save_date: Mapped[datetime] = mapped_column(Date, index=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)

    # All three are optional: a SAVE need not report a learning metric, and NULL
    # means "not stated in that SAVE". That is deliberately different from 0,
    # which is a counted zero. The columns were NOT NULL with default 0 until
    # revision 0006, which could only record the difference by losing it.
    wanikani_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bunpro_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    bunpro_points: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Progress inside the *source* level bar — not cumulative lifetime XP.
    source_character_level: Mapped[int] = mapped_column(Integer, default=1)
    source_character_rank: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Which SAVE schema wrote this row, and therefore what the two numbers
    # below mean. 1 = XP inside the current source level, which resets on
    # level-up. 2 = cumulative total since the level-2 baseline, which never
    # resets. Existing rows predate versioning and are all 1.
    save_version: Mapped[int] = mapped_column(Integer, default=1)
    # Deliberately reused rather than renamed: under version 1 this is the
    # level-internal value it always was, under version 2 the cumulative total.
    # `save_version` says which, and no historical row is rewritten.
    source_level_xp: Mapped[int] = mapped_column(Integer, default=0)
    source_level_xp_cap: Mapped[int] = mapped_column(Integer, default=0)
    # The optional rank boss event, as reported. Presence here is a record of
    # what the SAVE said; whether a reward was granted lives in
    # JapaneseRankReward, which is what makes granting idempotent.
    rank_boss_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    rank_boss_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    rank_reward_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    reported_session_xp: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    vocabulary_score: Mapped[int] = mapped_column(Integer, default=0)
    grammar_score: Mapped[int] = mapped_column(Integer, default=0)
    reading_score: Mapped[int] = mapped_column(Integer, default=0)
    listening_score: Mapped[int] = mapped_column(Integer, default=0)
    speaking_score: Mapped[int] = mapped_column(Integer, default=0)

    current_grammar_point: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    debuffs_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_vocabulary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    daily_quest_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    raw_save: Mapped[str] = mapped_column(Text)
    normalized_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    # baseline | progress | duplicate | historical | warning
    classification: Mapped[str] = mapped_column(String(20), default="baseline")
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)

    # Deterministic reward fields (added after the table shipped in PR #8, so
    # they are also listed in database._ADDED_COLUMNS for existing databases).
    session_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    session_completion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # baseline | deterministic_session | legacy_level_delta | historical
    # | duplicate | warning
    reward_calculation: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    stat_xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    # JSON object of {stat_key: xp} actually awarded for this import
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    warning_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QuestCompletion(Base):
    """One rewarded completion of a quest for one evaluated period.

    Append-only: rows are never edited or deleted, not through the UI and not
    by the services. The unique dedup_key is what actually prevents a
    repeatable quest from paying twice for the same period — the check is in
    the database, not only in a preceding Python query, so two near-simultaneous
    completions cannot both slip through.
    """

    __tablename__ = "quest_completions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quest_id: Mapped[int] = mapped_column(Integer, index=True)
    # Evaluated window. NULL for a one-off quest, which has no window.
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    stat_xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    # JSON object of {stat_key: xp} actually awarded, for auditing
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Deterministic key, e.g. "quest:17:weekly:2026-07-27" — see quests.py
    dedup_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HeroStat(Base):
    """Cumulative stat XP per attribute (feeds the future stats / radar screen)."""

    __tablename__ = "hero_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stat_key: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class StatXpEvent(Base):
    """Audit record for a single stat-XP award (separate from global XpEvent)."""

    __tablename__ = "stat_xp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stat_key: Mapped[str] = mapped_column(String(50), index=True)
    xp: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(50), default="habit")
    source_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# FitTrackee — endurance activities from an external, read-only source
# ---------------------------------------------------------------------------

class FitTrackeeSport(Base):
    """One sport as FitTrackee reports it, plus the user's endurance decision.

    Sport ids are per-instance and not stable across FitTrackee installations,
    so nothing here may be inferred from an id or from a name. A newly seen
    sport arrives with ``counts_for_endurance = False`` and stays inert until
    the user ticks it — which is also why a FitTrackee update that adds a sport
    needs no code change here.
    """

    __tablename__ = "fittrackee_sports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sport_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(100))
    # Whether FitTrackee still offers it. A retired sport keeps its history.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # The one user decision: does this sport count as endurance training?
    counts_for_endurance: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class FitTrackeeWorkout(Base):
    """One synchronised FitTrackee activity.

    Deliberately holds no GPS track, no coordinates, no map, no notes, no
    description and no media. None of that is needed to award XP, count a quest
    or deduplicate, and storing it would move private route data into this
    database for no purpose.

    ``reward_eligible`` is the baseline flag and is written exactly once, when
    the row is first imported. A workout that existed before the connection was
    made is history, not an achievement, and no later edit, sport activation or
    restart may turn it into XP.
    """

    __tablename__ = "fittrackee_workouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # FitTrackee workout ids are short strings, not integers.
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # Naive UTC, like every other timestamp in this app.
    workout_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # The calendar day in the application timezone, resolved once at import so
    # weekday quests and period windows never re-derive it differently.
    local_date: Mapped[date] = mapped_column(Date, index=True)

    sport_id: Mapped[int] = mapped_column(Integer, index=True)
    sport_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    moving_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distance_km: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ave_speed: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_speed: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Stored and displayed when present, never used to compute XP.
    ave_hr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    remote_modified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Sport enabled for endurance AND long enough. Recomputed on every change.
    qualifies_for_endurance: Mapped[bool] = mapped_column(Boolean, default=False)
    # False for everything imported by the baseline. Never flips to True.
    reward_eligible: Mapped[bool] = mapped_column(Boolean, default=False)

    source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        # The two questions every quest counter asks.
        Index("ix_ft_workouts_qualifying", "qualifies_for_endurance", "reward_eligible"),
        Index("ix_ft_workouts_local_date", "local_date"),
    )


class FitTrackeeConnection(Base):
    """The single row describing this Hero's link to one FitTrackee instance.

    Holds no token and no secret — those live in a file-backed store outside the
    database, so a database backup can never carry a usable credential.
    """

    __tablename__ = "fittrackee_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Set by the first successful baseline import. Until then nothing is
    # reward-eligible, because there is no "before" to compare against.
    baseline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    baseline_workouts: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sports_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Sanitized text only — never a token, never a raw exception.
    last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    needs_reauth: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class JapaneseRankReward(Base):
    """One permanently unlocked rank-boss reward.

    Separate from the snapshot on purpose. Deduplicating rewards by SAVE hash
    would fail the moment the same boss is reported in a differently worded
    SAVE — a re-export, a corrected line, a second paste — and would hand out
    the badge twice. The unique boss id is the guarantee instead, enforced by
    the database rather than by a preceding query.

    A reward is a milestone, never XP: the progression that reached the level
    was already paid for by the XP that got there.
    """

    __tablename__ = "japanese_rank_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    boss_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    reward_id: Mapped[str] = mapped_column(String(50))
    reward_name: Mapped[str] = mapped_column(String(100))
    boss_title: Mapped[str] = mapped_column(String(200))
    source_level: Mapped[int] = mapped_column(Integer)
    tier: Mapped[int] = mapped_column(Integer)
    # Which import first reported it, for the audit trail.
    import_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
