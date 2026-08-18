"""FitTrackee activities become endurance XP — once, and never retroactively.

The acceptance criteria of this integration live here: a baseline pays nothing,
a new qualifying unit pays exactly 40 + 40, the same sync twice pays once, and
only sports the user enabled count at all.

Every workout in this file is synthetic. No real activity, no real id, no real
route, no real instance.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.fittrackee_sync import (
    ENDURANCE_STAT_KEY,
    ENDURANCE_STAT_XP,
    MIN_ENDURANCE_SECONDS,
    WORKOUT_XP,
    NormalizationError,
    apply_workouts,
    endurance_sport_ids,
    get_connection,
    normalize_workout,
    qualifies,
    source_id_for,
    store_sports,
)
from app.models import (
    Base,
    FitTrackeeSport,
    FitTrackeeWorkout,
    HeroProfile,
    HeroStat,
    StatXpEvent,
    XpEvent,
)

RUNNING = 5
CYCLING = 1


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(HeroProfile(name="Hero", level=1, total_xp=0))
    session.commit()
    yield session
    session.close()


def workout(
    external_id="ft-1",
    sport_id=RUNNING,
    when="Mon, 17 Aug 2026 06:30:00 GMT",
    duration="0:42:10",
    moving="0:39:55",
    distance=7.2,
    **extra,
):
    """A synthetic record shaped like the documented FitTrackee response."""
    payload = {
        "id": external_id,
        "sport_id": sport_id,
        "workout_date": when,
        "duration": duration,
        "moving": moving,
        "distance": distance,
        "ave_speed": 10.8,
        "max_speed": 14.7,
        "ave_hr": 142,
        "max_hr": 167,
        "modification_date": None,
    }
    payload.update(extra)
    return payload


def enable(db, *sport_ids, label="Running"):
    for sport_id in sport_ids:
        db.add(
            FitTrackeeSport(
                sport_id=sport_id, label=label, counts_for_endurance=True
            )
        )
    db.commit()


def totals(db):
    hero = db.query(HeroProfile).one()
    stat = db.query(HeroStat).filter(HeroStat.stat_key == ENDURANCE_STAT_KEY).first()
    return hero.total_xp, (stat.xp if stat else 0)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_a_documented_record_normalises():
    n = normalize_workout(workout())
    assert n.external_id == "ft-1"
    assert n.sport_id == RUNNING
    assert n.duration_seconds == 2530
    assert n.moving_seconds == 2395
    assert n.workout_at == datetime(2026, 8, 17, 6, 30)


def test_moving_time_wins_when_present():
    assert normalize_workout(workout()).effective_seconds == 2395


def test_duration_stands_in_when_moving_is_missing():
    n = normalize_workout(workout(moving=None))
    assert n.moving_seconds is None
    assert n.effective_seconds == 2530


def test_a_missing_distance_is_not_invented():
    assert normalize_workout(workout(distance=None)).distance_km is None


def test_missing_heart_rate_is_fine():
    n = normalize_workout(workout(ave_hr=None, max_hr=None))
    assert n.ave_hr is None and n.max_hr is None


@pytest.mark.parametrize("bad", [None, "", "später", "abc"])
def test_an_unreadable_duration_is_refused(bad):
    """Never guessed at — a guess could turn a 2-minute walk into a session."""
    with pytest.raises(NormalizationError):
        normalize_workout(workout(duration=bad))


def test_a_workout_without_an_id_is_refused():
    with pytest.raises(NormalizationError):
        normalize_workout(workout(external_id=None))


def test_a_workout_without_a_date_is_refused():
    with pytest.raises(NormalizationError):
        normalize_workout(workout(when=None))


def test_the_hash_is_stable_across_calls():
    assert normalize_workout(workout()).source_hash == normalize_workout(workout()).source_hash


def test_the_hash_changes_with_the_duration():
    a = normalize_workout(workout()).source_hash
    b = normalize_workout(workout(duration="1:02:10")).source_hash
    assert a != b


def test_the_hash_ignores_fields_hero_does_not_keep():
    """A renamed workout or an added note is not a reward-relevant change —
    and those fields never reach this layer in the first place."""
    a = normalize_workout(workout()).source_hash
    b = normalize_workout(workout(title="Morgenlauf", notes="war schwer"))
    assert a == b.source_hash


def test_no_private_field_survives_normalisation():
    n = normalize_workout(
        workout(notes="privat", title="Heimweg", map="abc", bounds=[1, 2, 3, 4])
    )
    blob = repr(n)
    for leaked in ("privat", "Heimweg", "abc", "bounds"):
        assert leaked not in blob


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

def test_the_threshold_is_ten_minutes():
    assert MIN_ENDURANCE_SECONDS == 600


def test_exactly_ten_minutes_qualifies():
    assert qualifies(RUNNING, 600, {RUNNING})


def test_one_second_short_does_not():
    assert not qualifies(RUNNING, 599, {RUNNING})


def test_a_long_session_of_a_disabled_sport_does_not():
    assert not qualifies(CYCLING, 7200, {RUNNING})


def test_no_enabled_sport_means_nothing_qualifies():
    assert not qualifies(RUNNING, 7200, set())


def test_enabled_sports_come_from_the_users_choice(db):
    db.add(FitTrackeeSport(sport_id=RUNNING, label="Running", counts_for_endurance=True))
    db.add(FitTrackeeSport(sport_id=CYCLING, label="Cycling", counts_for_endurance=False))
    db.commit()
    assert endurance_sport_ids(db) == {RUNNING}


def test_a_newly_seen_sport_is_inert(db):
    """Nothing is inferred from a name or an id — the user enables it."""
    store_sports(db, [{"sport_id": 9, "label": "Running", "is_active": True}])
    db.commit()
    row = db.query(FitTrackeeSport).filter(FitTrackeeSport.sport_id == 9).one()
    assert row.counts_for_endurance is False


def test_refreshing_sports_keeps_the_users_decision(db):
    enable(db, RUNNING)
    store_sports(db, [{"sport_id": RUNNING, "label": "Laufen", "is_active": True}])
    db.commit()
    row = db.query(FitTrackeeSport).filter(FitTrackeeSport.sport_id == RUNNING).one()
    assert row.counts_for_endurance is True
    assert row.label == "Laufen"      # the label does follow FitTrackee


# ---------------------------------------------------------------------------
# Baseline — the first import pays nothing
# ---------------------------------------------------------------------------

def test_a_baseline_of_ten_workouts_awards_nothing(db):
    enable(db, RUNNING)
    batch = [workout(external_id=f"ft-{n}") for n in range(10)]

    outcome = apply_workouts(db, batch, baseline=True)
    db.commit()

    assert db.query(FitTrackeeWorkout).count() == 10
    assert db.query(XpEvent).count() == 0
    assert db.query(StatXpEvent).count() == 0
    assert totals(db) == (0, 0)
    assert outcome.xp_awarded == 0


def test_baseline_workouts_are_marked_ineligible(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout()], baseline=True)
    db.commit()
    assert db.query(FitTrackeeWorkout).one().reward_eligible is False


def test_a_baseline_still_records_qualification(db):
    """The row is honest about what it is; only the reward is withheld."""
    enable(db, RUNNING)
    apply_workouts(db, [workout()], baseline=True)
    db.commit()
    assert db.query(FitTrackeeWorkout).one().qualifies_for_endurance is True


def test_a_second_baseline_run_changes_nothing(db):
    enable(db, RUNNING)
    batch = [workout(external_id=f"ft-{n}") for n in range(10)]
    apply_workouts(db, batch, baseline=True)
    db.commit()

    outcome = apply_workouts(db, batch, baseline=True)
    db.commit()

    assert outcome.unchanged == 10
    assert outcome.new == 0
    assert totals(db) == (0, 0)


def test_editing_a_baseline_workout_never_pays(db):
    """Rule 19: baseline stays baseline, whatever happens to the record."""
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:20:00", moving="0:20:00")], baseline=True)
    db.commit()

    apply_workouts(db, [workout(duration="1:20:00", moving="1:20:00")], baseline=False)
    db.commit()

    row = db.query(FitTrackeeWorkout).one()
    assert row.reward_eligible is False
    assert row.duration_seconds == 4800        # the edit is stored
    assert totals(db) == (0, 0)                # but pays nothing


def test_enabling_a_sport_later_does_not_pay_for_the_baseline(db):
    """Rule 45: sport activation only affects future, unjudged workouts."""
    store_sports(db, [{"sport_id": RUNNING, "label": "Running", "is_active": True}])
    apply_workouts(db, [workout()], baseline=True)
    db.commit()
    assert totals(db) == (0, 0)

    enable(db, CYCLING)  # unrelated
    db.query(FitTrackeeSport).filter(
        FitTrackeeSport.sport_id == RUNNING
    ).one().counts_for_endurance = True
    db.commit()

    apply_workouts(db, [workout()], baseline=False)
    db.commit()

    assert totals(db) == (0, 0)


# ---------------------------------------------------------------------------
# A new workout after the baseline
# ---------------------------------------------------------------------------

def test_a_new_qualifying_workout_pays_forty_and_forty(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(external_id="old")], baseline=True)
    db.commit()

    outcome = apply_workouts(db, [workout(external_id="new")], baseline=False)
    db.commit()

    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)
    assert outcome.xp_awarded == WORKOUT_XP
    assert outcome.stat_xp_awarded == ENDURANCE_STAT_XP


def test_it_writes_exactly_one_row_in_each_ledger(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout()], baseline=False)
    db.commit()

    assert db.query(XpEvent).count() == 1
    assert db.query(StatXpEvent).count() == 1


def test_the_ledger_rows_carry_the_stable_source_id(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(external_id="abc")], baseline=False)
    db.commit()

    assert db.query(XpEvent).one().source_id == source_id_for("abc")
    assert db.query(XpEvent).one().source == "fittrackee"
    assert db.query(StatXpEvent).one().stat_key == ENDURANCE_STAT_KEY


def test_the_same_sync_twice_pays_once(db):
    """Acceptance test 3."""
    enable(db, RUNNING)
    batch = [workout()]
    apply_workouts(db, batch, baseline=False)
    db.commit()

    outcome = apply_workouts(db, batch, baseline=False)
    db.commit()

    assert outcome.xp_awarded == 0
    assert outcome.unchanged == 1
    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)
    assert db.query(XpEvent).count() == 1


def test_ten_repeated_syncs_still_pay_once(db):
    enable(db, RUNNING)
    for _ in range(10):
        apply_workouts(db, [workout()], baseline=False)
        db.commit()
    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)


def test_a_short_workout_is_stored_but_pays_nothing(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:08:00", moving="0:07:30")], baseline=False)
    db.commit()

    row = db.query(FitTrackeeWorkout).one()
    assert row.qualifies_for_endurance is False
    assert totals(db) == (0, 0)


def test_a_disabled_sport_is_stored_but_pays_nothing(db):
    """Rule 45, first half."""
    enable(db, RUNNING)
    apply_workouts(db, [workout(sport_id=CYCLING)], baseline=False)
    db.commit()

    assert db.query(FitTrackeeWorkout).count() == 1
    assert totals(db) == (0, 0)


def test_a_broken_workout_does_not_stop_the_batch(db):
    enable(db, RUNNING)
    outcome = apply_workouts(
        db,
        [workout(external_id="good"), workout(external_id="bad", duration="???")],
        baseline=False,
    )
    db.commit()

    assert outcome.skipped == 1
    assert db.query(FitTrackeeWorkout).count() == 1
    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_a_lengthened_workout_keeps_exactly_one_award(db):
    """20 minutes becomes 45: still one award, still 40 XP net."""
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:20:00", moving="0:20:00")], baseline=False)
    db.commit()

    apply_workouts(db, [workout(duration="0:45:00", moving="0:45:00")], baseline=False)
    db.commit()

    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)
    assert db.query(XpEvent).count() == 1
    assert db.query(StatXpEvent).count() == 1


def test_shortening_below_the_threshold_takes_the_award_back(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:45:00", moving="0:45:00")], baseline=False)
    db.commit()
    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)

    apply_workouts(db, [workout(duration="0:05:00", moving="0:05:00")], baseline=False)
    db.commit()

    row = db.query(FitTrackeeWorkout).one()
    assert row.qualifies_for_endurance is False
    assert totals(db) == (0, 0)
    assert db.query(XpEvent).count() == 0
    assert db.query(StatXpEvent).count() == 0


def test_the_workout_survives_losing_its_award(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:45:00", moving="0:45:00")], baseline=False)
    db.commit()
    apply_workouts(db, [workout(duration="0:05:00", moving="0:05:00")], baseline=False)
    db.commit()
    assert db.query(FitTrackeeWorkout).count() == 1


def test_requalifying_pays_again_but_only_once(db):
    enable(db, RUNNING)
    for duration in ("0:45:00", "0:05:00", "0:45:00"):
        apply_workouts(db, [workout(duration=duration, moving=duration)], baseline=False)
        db.commit()

    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)
    assert db.query(XpEvent).count() == 1


def test_no_orphan_audit_rows_survive_a_change(db):
    enable(db, RUNNING)
    apply_workouts(db, [workout(duration="0:20:00", moving="0:20:00")], baseline=False)
    db.commit()
    apply_workouts(db, [workout(duration="0:45:00", moving="0:45:00")], baseline=False)
    db.commit()

    source_id = source_id_for("ft-1")
    assert db.query(XpEvent).filter(XpEvent.source_id == source_id).count() == 1
    assert db.query(StatXpEvent).filter(StatXpEvent.source_id == source_id).count() == 1


def test_a_missing_workout_is_not_revoked(db):
    """Rule 17: absence from one fetch is not proof of deletion."""
    enable(db, RUNNING)
    apply_workouts(db, [workout()], baseline=False)
    db.commit()

    apply_workouts(db, [], baseline=False)
    db.commit()

    assert db.query(FitTrackeeWorkout).count() == 1
    assert totals(db) == (WORKOUT_XP, ENDURANCE_STAT_XP)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_a_dry_run_writes_nothing(db):
    enable(db, RUNNING)
    outcome = apply_workouts(db, [workout()], baseline=False, dry_run=True)
    db.rollback()

    assert outcome.xp_awarded == WORKOUT_XP     # it says what would happen
    assert db.query(FitTrackeeWorkout).count() == 0
    assert db.query(XpEvent).count() == 0
    assert totals(db) == (0, 0)


def test_a_dry_run_reports_the_split(db):
    enable(db, RUNNING)
    outcome = apply_workouts(
        db,
        [workout(external_id="a"), workout(external_id="b", duration="0:03:00", moving="0:03:00")],
        baseline=False,
        dry_run=True,
    )
    db.rollback()

    assert outcome.qualifying == 1
    assert outcome.not_qualifying == 1
    assert outcome.new == 2


def test_the_dry_run_summary_leaks_nothing(db):
    enable(db, RUNNING)
    outcome = apply_workouts(db, [workout(notes="privat")], baseline=False, dry_run=True)
    db.rollback()
    assert "privat" not in "\n".join(outcome.as_lines())


# ---------------------------------------------------------------------------
# Hero level and connection state
# ---------------------------------------------------------------------------

def test_the_hero_level_follows_the_award(db):
    enable(db, RUNNING)
    batch = [workout(external_id=f"ft-{n}") for n in range(40)]
    apply_workouts(db, batch, baseline=False)
    db.commit()

    hero = db.query(HeroProfile).one()
    assert hero.total_xp == 40 * WORKOUT_XP
    assert hero.level > 1


def test_the_connection_row_is_created_once(db):
    first = get_connection(db)
    db.commit()
    assert get_connection(db).id == first.id


def test_the_local_date_is_resolved_at_import(db):
    """Late Monday UTC is Tuesday in Europe/Berlin, and the row must say so."""
    enable(db, RUNNING)
    apply_workouts(
        db, [workout(when="Mon, 17 Aug 2026 23:30:00 GMT")], baseline=False
    )
    db.commit()

    row = db.query(FitTrackeeWorkout).one()
    assert row.workout_at == datetime(2026, 8, 17, 23, 30)
    assert row.local_date.isoweekday() == 2
