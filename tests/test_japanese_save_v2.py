"""SAVE version 2: cumulative XP, the version bridge, and rank boss rewards.

The bug this fixes came from the coach switching to cumulative totals while
wger-hero still read the bar as level-internal XP, so a total that had passed
the current level's threshold looked implausible and paid nothing.

The fix is the version marker plus the cross-version bridge, **not** tolerating
a contradictory bar. `Lv 2 | 1038 / 1000` is internally inconsistent under
version 2 — 1038 cumulative XP is already past level 3's threshold of 1000 —
and it is treated as a defect to surface, not as a normal state. Legitimising it
would hide broken coach exports instead of finding them.

So the first thing tested here is that a version-2 SAVE is read with version-2
semantics, the second is that a version-1 SAVE is not, and the third is that a
version-2 SAVE has to agree with the curve.

Every SAVE below is synthetic.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.japanese_import import import_save, rank_reward_state
from app.japanese_levels import BASELINE_LEVEL
from app.japanese_saves import (
    CALC_BRIDGE,
    CALC_CUMULATIVE,
    CALC_LEGACY,
    SAVE_VERSION_CUMULATIVE,
    SAVE_VERSION_LEGACY,
    PreviousState,
    SaveParseError,
    calculate_delta,
    parse_save,
)
from app.models import (
    Base,
    HeroProfile,
    JapaneseRankReward,
    JapaneseSaveImport,
    StatXpEvent,
    XpEvent,
)


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


def save_text(
    *,
    version=2,
    level=3,
    rank="修行者",
    xp=1038,
    cap=2100,
    day="2026-08-19",
    streak=23,
    boss=None,
    status=None,
    reward_id=None,
    reward_name=None,
    session_xp=None,
    extra_lines=(),
):
    lines = ["=== 状態 SAVE ==="]
    if version is not None:
        lines.append(f"SAVE-Version: {version}")
    lines += [
        f"Datum: {day} | Streak: {streak}",
        "WaniKani-Level: 2",
        "Bunpro-Level: N5",
        "Grammatikpunkte im SRS: 22",
        f"Charakter: Lv {level} ({rank}) | {xp} / {cap} XP",
        "語彙 295 | 文法 590 | 読解 10 | 聴解 0 | 会話 355",
        "Aktueller Grammatikpunkt: と",
        "Debuffs: keine",
        "Neue Vokabeln heute: keine",
        "Tagesquest: Beispiel",
    ]
    if session_xp is not None:
        lines.append(f"Session-XP: {session_xp}")
    if boss is not None:
        lines.append(f"Rangstufenboss-ID: {boss}")
    if status is not None:
        lines.append(f"Rangstufenboss-Status: {status}")
    if reward_id is not None:
        lines.append(f"Rangbelohnung-ID: {reward_id}")
    if reward_name is not None:
        lines.append(f"Rangbelohnung: {reward_name}")
    lines += list(extra_lines)
    lines.append("=== END SAVE ===")
    return "\n".join(lines)


def legacy_previous(level=2, xp=708, cap=1000, day=date(2026, 8, 7)):
    return PreviousState(
        character_level=level, level_xp=xp, level_xp_cap=cap, save_date=day
    )


def v2_previous(xp, level=3, cap=2100, day=date(2026, 8, 18)):
    return PreviousState(
        character_level=level,
        level_xp=xp,
        level_xp_cap=cap,
        save_date=day,
        save_version=SAVE_VERSION_CUMULATIVE,
    )


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def test_a_save_without_a_version_marker_is_version_one():
    parsed = parse_save(save_text(version=None, level=2, rank="見習い", xp=708, cap=1000))
    assert parsed.save_version == SAVE_VERSION_LEGACY
    assert parsed.is_cumulative is False


def test_a_marked_save_is_version_two():
    parsed = parse_save(save_text())
    assert parsed.save_version == SAVE_VERSION_CUMULATIVE
    assert parsed.is_cumulative is True


def test_an_unknown_version_is_refused_not_guessed():
    """Reading a future format with today's rules is how a wrong reward is paid."""
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(save_text(version=3))
    assert any("nicht unterstützt" in e.message for e in excinfo.value.errors)


def test_a_non_numeric_version_is_refused():
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(save_text(version="zwei"))
    assert any("keine Zahl" in e.message for e in excinfo.value.errors)


def test_only_a_version_two_save_reports_a_total():
    assert parse_save(save_text()).total_xp == 1038
    assert parse_save(save_text(version=None, level=2, rank="見習い")).total_xp is None


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_the_reported_case_is_fixed_by_the_bridge_not_by_tolerating_bad_data():
    """The user's stuck SAVE, in its *correct* version-2 form.

    The coach's next export states level 3 with the level-4 threshold, which is
    what 1038 cumulative XP actually is. That is the SAVE the bridge pays out.
    """
    parsed = parse_save(save_text(level=3, rank="修行者", xp=1038, cap=2100))
    result = calculate_delta(parsed, legacy_previous())

    assert result.xp_delta == 330
    assert result.classification != "warning"


def test_a_contradictory_version_two_save_is_not_a_normal_state():
    """`Lv 2 | 1038 / 1000` claims level 2 while holding level 3's threshold.

    It must not pass silently: a coach export that says this is broken, and
    accepting it would hide the defect rather than surface it.
    """
    parsed = parse_save(save_text(level=2, rank="見習い", xp=1038, cap=1000))
    result = calculate_delta(parsed, legacy_previous())

    assert result.warning is not None
    assert "passt nicht zu 1038 Gesamt-XP" in result.warning
    assert "Level 3" in result.warning


def test_the_contradictory_save_still_follows_its_total():
    """The XP is not withheld. The total is the part that is almost certainly
    right, and withholding it would repeat the very failure this schema was
    introduced to fix — the user would be stuck at 0 XP again."""
    parsed = parse_save(save_text(level=2, rank="見習い", xp=1038, cap=1000))
    result = calculate_delta(parsed, legacy_previous())
    assert result.xp_delta == 330


def test_a_valid_version_two_save_carries_no_curve_warning():
    result = calculate_delta(
        parse_save(save_text(level=3, rank="修行者", xp=1038, cap=2100)),
        v2_previous(1000),
    )
    assert result.warning is None


@pytest.mark.parametrize(
    "level,rank,xp,cap,expected_fragment",
    [
        (2, "見習い", 1038, 1000, "passt nicht zu 1038 Gesamt-XP"),
        (3, "見習い", 1038, 2100, "Rang"),
        (3, "修行者", 1038, 9999, "nächste Schwelle"),
        (99, "修行者", 1038, 2100, "außerhalb der Kurve"),
    ],
)
def test_every_kind_of_curve_disagreement_is_reported(
    level, rank, xp, cap, expected_fragment
):
    result = calculate_delta(
        parse_save(save_text(level=level, rank=rank, xp=xp, cap=cap)), v2_previous(1000)
    )
    assert expected_fragment in (result.warning or "")


def test_a_legacy_save_is_never_checked_against_the_curve():
    """The curve describes cumulative totals. A level-internal bar would fail it
    for no reason, so version 1 is left entirely alone."""
    result = calculate_delta(
        parse_save(save_text(version=None, level=2, rank="見習い", xp=808, cap=1000)),
        legacy_previous(xp=708),
    )
    assert result.xp_delta == 100
    assert result.warning is None


def test_the_same_bar_under_version_one_still_warns():
    """Version 1 semantics are untouched — the guard was right for them."""
    parsed = parse_save(save_text(version=None, level=2, rank="見習い", xp=1038, cap=1000))
    result = calculate_delta(parsed, legacy_previous())

    assert result.xp_delta == 0
    assert "über der Obergrenze" in (result.warning or "")


# ---------------------------------------------------------------------------
# Version-2 deltas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "old,new,cap,expected",
    [
        (1038, 1068, 2100, 30),
        (2090, 2120, 3300, 30),
        (3290, 3335, 4600, 45),
        (1038, 1038, 2100, 0),
    ],
)
def test_the_delta_is_a_plain_difference(old, new, cap, expected):
    result = calculate_delta(parse_save(save_text(xp=new, cap=cap)), v2_previous(old))
    assert result.xp_delta == expected
    assert result.reward_calculation == CALC_CUMULATIVE


def test_a_valid_level_up_gives_the_plain_difference():
    """Lv 3 2090/2100 → Lv 4 2120/3300 = +30.

    Both sides are internally consistent against the curve, so this is the
    reference case for a version-2 level change. The old level-change formula
    would have given (2100-2090)+2120 = 2130.
    """
    result = calculate_delta(
        parse_save(save_text(level=4, rank="探究者", xp=2120, cap=3300)),
        v2_previous(2090, level=3, cap=2100),
    )
    assert result.xp_delta == 30
    assert result.reward_calculation == CALC_CUMULATIVE
    assert result.warning is None


def test_crossing_several_levels_at_once_still_works():
    """A cumulative difference stays well defined however many thresholds it
    passes — unlike the level-internal formula, which refused a jump."""
    result = calculate_delta(
        parse_save(save_text(level=6, rank="旅人", xp=4700, cap=6000)),
        v2_previous(1038),
    )
    assert result.xp_delta == 3662


def test_a_falling_total_awards_nothing_and_deducts_nothing():
    result = calculate_delta(parse_save(save_text(xp=900, cap=2100)), v2_previous(1500))
    assert result.xp_delta == 0
    assert result.classification == "warning"
    assert "gesunken" in result.warning


# ---------------------------------------------------------------------------
# The cross-version bridge
# ---------------------------------------------------------------------------

def test_the_documented_bridge_example():
    """Legacy Lv 2 708/1000 → v2 Lv 3 1038/2100 = +330."""
    result = calculate_delta(parse_save(save_text()), legacy_previous(xp=708))

    assert result.xp_delta == 330
    assert result.reward_calculation == CALC_BRIDGE


def test_the_bridge_does_not_use_the_level_change_formula():
    """(1000 - 708) + 1038 would be 1330 — the old formula assumes the new bar
    restarted at zero, which under version 2 it does not."""
    result = calculate_delta(parse_save(save_text()), legacy_previous(xp=708))
    assert result.xp_delta != 1330


def test_the_bridge_says_what_it_did():
    result = calculate_delta(parse_save(save_text()), legacy_previous(xp=708))
    assert "SAVE-Version 2" in result.warning
    assert "Baseline" in result.warning


def test_the_bridge_only_trusts_the_baseline_level():
    """From any other legacy level the cumulative equivalent is unknowable."""
    result = calculate_delta(parse_save(save_text()), legacy_previous(level=4, xp=500))
    assert result.xp_delta == 0
    assert result.classification == "warning"
    assert "nicht rekonstruierbar" in result.warning


def test_an_explicit_session_xp_rescues_an_unmappable_bridge():
    result = calculate_delta(
        parse_save(save_text(session_xp=42)), legacy_previous(level=4, xp=500)
    )
    assert result.xp_delta == 42
    assert result.reward_calculation == CALC_BRIDGE


def test_a_bridge_that_would_go_backwards_awards_nothing():
    result = calculate_delta(parse_save(save_text(xp=600)), legacy_previous(xp=708))
    assert result.xp_delta == 0
    assert result.classification == "warning"


def test_the_baseline_level_is_the_one_the_campaign_started_at():
    assert BASELINE_LEVEL == 2


# ---------------------------------------------------------------------------
# Version 1 regressions — none of the above may change them
# ---------------------------------------------------------------------------

def test_two_legacy_saves_use_the_same_level_delta():
    result = calculate_delta(
        parse_save(save_text(version=None, level=2, rank="見習い", xp=808, cap=1000)),
        legacy_previous(xp=708),
    )
    assert result.xp_delta == 100
    assert result.reward_calculation == CALC_LEGACY


def test_a_legacy_level_up_keeps_its_old_formula():
    """(1000 - 708) + 40 = 332, which is right for level-internal counting."""
    result = calculate_delta(
        parse_save(save_text(version=None, level=3, rank="修行者", xp=40, cap=2100)),
        legacy_previous(xp=708),
    )
    assert result.xp_delta == 332


def test_a_legacy_save_after_a_version_two_save_is_still_read_as_legacy():
    parsed = parse_save(save_text(version=None, level=3, rank="修行者", xp=40, cap=2100))
    assert parsed.is_cumulative is False


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_the_version_is_part_of_the_hash():
    """Identical figures under different versions are different snapshots,
    because the numbers mean different things."""
    v1 = parse_save(save_text(version=None, level=3, rank="修行者", xp=1038, cap=2100))
    v2 = parse_save(save_text(version=2, level=3, rank="修行者", xp=1038, cap=2100))
    assert v1.normalized_hash() != v2.normalized_hash()


def test_a_boss_event_is_part_of_the_hash():
    plain = parse_save(save_text(level=5, rank="挑戦者", xp=3350, cap=4600))
    with_boss = parse_save(
        save_text(level=5, rank="挑戦者", xp=3350, cap=4600,
                  boss="jp-rank-boss-01", status="bestanden")
    )
    assert plain.normalized_hash() != with_boss.normalized_hash()


def test_the_same_save_still_hashes_the_same():
    assert parse_save(save_text()).normalized_hash() == parse_save(save_text()).normalized_hash()


# ---------------------------------------------------------------------------
# Rank boss rewards through a real import
# ---------------------------------------------------------------------------

BOSS_SAVE = dict(
    level=5, rank="挑戦者", xp=3350, cap=4600,
    boss="jp-rank-boss-01", status="bestanden",
    reward_id="jp-rank-reward-01", reward_name="旅立ちの証",
)


def _seed_v2_baseline(db, xp=3300, day="2026-08-18"):
    """An already-imported version-2 snapshot to measure the next delta from."""
    db.add(
        JapaneseSaveImport(
            save_date=date.fromisoformat(day),
            streak=1,
            source_character_level=5,
            source_character_rank="挑戦者",
            source_level_xp=xp,
            source_level_xp_cap=4600,
            save_version=SAVE_VERSION_CUMULATIVE,
            raw_save="seed",
            normalized_hash=f"seed-{xp}",
            classification="progress",
            xp_awarded=0,
            created_at=datetime(2026, 8, 18, 12, 0),
        )
    )
    db.commit()


def test_a_passed_boss_grants_its_reward(db):
    _seed_v2_baseline(db)
    result = import_save(db, save_text(**BOSS_SAVE))

    assert result.rank_reward_granted is True
    reward = db.query(JapaneseRankReward).one()
    assert reward.boss_id == "jp-rank-boss-01"
    assert reward.reward_id == "jp-rank-reward-01"
    assert reward.reward_name == "旅立ちの証"


def test_the_reward_carries_no_xp_of_its_own(db):
    """Reaching the level was already paid for by the XP that got there."""
    _seed_v2_baseline(db)
    result = import_save(db, save_text(**BOSS_SAVE))

    assert result.xp_awarded == 50          # 3350 - 3300, the normal delta
    assert db.query(XpEvent).count() == 1   # not two


def test_reimporting_the_same_save_grants_nothing_twice(db):
    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))
    second = import_save(db, save_text(**BOSS_SAVE))

    assert second.is_duplicate is True
    assert db.query(JapaneseRankReward).count() == 1


def test_a_differently_worded_save_grants_nothing_twice(db):
    """The case a SAVE-hash check alone would miss: same boss, different text."""
    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))

    later = dict(BOSS_SAVE, xp=3400, day="2026-08-20", streak=99)
    result = import_save(db, save_text(**later))

    assert result.is_duplicate is False        # a genuinely new snapshot
    assert result.rank_reward_granted is False
    assert result.rank_reward_already_held is True
    assert db.query(JapaneseRankReward).count() == 1


def test_the_normal_save_still_pays_when_the_reward_is_already_held(db):
    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))

    later = dict(BOSS_SAVE, xp=3400, day="2026-08-20")
    result = import_save(db, save_text(**later))

    assert result.xp_awarded == 50             # 3400 - 3350
    assert result.rank_reward_granted is False


def test_a_second_boss_can_still_be_granted_later(db):
    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))

    second = dict(
        level=10, rank="熟練者", xp=10850, cap=12600, day="2026-09-01",
        boss="jp-rank-boss-02", status="bestanden",
        reward_id="jp-rank-reward-02", reward_name="修行の証",
    )
    result = import_save(db, save_text(**second))

    assert result.rank_reward_granted is True
    assert db.query(JapaneseRankReward).count() == 2


@pytest.mark.parametrize(
    "broken",
    [
        dict(boss="jp-rank-boss-99", status="bestanden"),
        dict(boss="jp-rank-boss-01", status="vielleicht"),
        dict(boss="jp-rank-boss-01", status="bestanden", reward_id="jp-rank-reward-02"),
        dict(boss="jp-rank-boss-01", status="bestanden", reward_name="修行の証"),
        dict(boss="jp-rank-boss-01"),
        dict(status="bestanden"),
        dict(reward_id="jp-rank-reward-01"),
    ],
)
def test_a_broken_boss_event_grants_nothing(db, broken):
    _seed_v2_baseline(db)
    fields = dict(level=5, rank="挑戦者", xp=3350, cap=4600, **broken)
    result = import_save(db, save_text(**fields))

    assert result.rank_reward_granted is False
    assert db.query(JapaneseRankReward).count() == 0


def test_a_broken_boss_event_does_not_cost_the_rest_of_the_save(db):
    """The snapshot and its XP survive a malformed optional line."""
    _seed_v2_baseline(db)
    result = import_save(
        db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600,
                      boss="jp-rank-boss-99", status="bestanden")
    )

    assert result.created is not None
    assert result.xp_awarded == 50
    assert result.rank_problems


def test_a_failed_boss_grants_nothing_and_is_no_error(db):
    _seed_v2_baseline(db)
    result = import_save(
        db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600,
                      boss="jp-rank-boss-01", status="nicht bestanden")
    )

    assert result.rank_reward_granted is False
    assert result.rank_problems == []
    assert db.query(JapaneseRankReward).count() == 0


def test_a_save_without_a_boss_line_is_unaffected(db):
    _seed_v2_baseline(db)
    result = import_save(db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600))

    assert result.rank_boss is None
    assert result.rank_problems == []
    assert db.query(JapaneseRankReward).count() == 0


def test_the_preview_state_writes_nothing(db):
    _seed_v2_baseline(db)
    state = rank_reward_state(db, parse_save(save_text(**BOSS_SAVE)))

    assert state.boss.boss_id == "jp-rank-boss-01"
    assert state.already_held is False
    assert db.query(JapaneseRankReward).count() == 0


def test_the_preview_reports_an_already_held_reward(db):
    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))

    state = rank_reward_state(db, parse_save(save_text(**dict(BOSS_SAVE, xp=3400))))
    assert state.already_held is True


# ---------------------------------------------------------------------------
# The import stores what it read
# ---------------------------------------------------------------------------

def test_the_import_records_the_save_version(db):
    _seed_v2_baseline(db)
    result = import_save(db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600))
    assert result.created.save_version == SAVE_VERSION_CUMULATIVE


def test_a_legacy_import_is_recorded_as_version_one(db):
    result = import_save(
        db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000)
    )
    assert result.created.save_version == SAVE_VERSION_LEGACY


def test_the_import_records_the_boss_event(db):
    _seed_v2_baseline(db)
    result = import_save(db, save_text(**BOSS_SAVE))

    assert result.created.rank_boss_id == "jp-rank-boss-01"
    assert result.created.rank_boss_status == "bestanden"
    assert result.created.rank_reward_id == "jp-rank-reward-01"


def test_a_version_two_import_awards_no_attribute_xp(db):
    """Same rule as the legacy path, for the same reason: the amount comes
    from a bar the coach maintains, not from something wger-hero derived, so
    it pays global XP but must not reach the attribute radar. Only a fully
    specified session mode and completion does that."""
    _seed_v2_baseline(db)
    result = import_save(db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600))

    assert result.xp_awarded == 50
    assert db.query(StatXpEvent).count() == 0


def test_a_version_two_save_with_a_session_line_does_award_attribute_xp(db):
    """The deterministic path still wins over the bar when both are present."""
    _seed_v2_baseline(db)
    text = save_text(
        level=5, rank="挑戦者", xp=3350, cap=4600,
        extra_lines=("Session-Modus: GENKI", "Session-Abschluss: vollständig"),
    )
    import_save(db, text)
    assert db.query(StatXpEvent).count() > 0


def test_the_hero_total_moves_by_the_delta_only(db):
    _seed_v2_baseline(db)
    import_save(db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600))

    hero = db.query(HeroProfile).one()
    assert hero.total_xp == 50      # never the cumulative 3350


# ---------------------------------------------------------------------------
# The preview screen
# ---------------------------------------------------------------------------

def test_the_preview_reports_curve_problems(db):
    from app.japanese_import import preview_save

    _seed_v2_baseline(db)
    preview = preview_save(db, save_text(level=9, rank="使い手", xp=3350, cap=10800))

    assert preview.curve_problems
    assert any("passt nicht zu 3350 Gesamt-XP" in p for p in preview.curve_problems)


def test_a_consistent_version_two_save_has_no_curve_problems(db):
    from app.japanese_import import preview_save

    _seed_v2_baseline(db)
    preview = preview_save(db, save_text(level=5, rank="挑戦者", xp=3350, cap=4600))
    assert preview.curve_problems == []


def test_a_legacy_save_is_not_checked_against_the_curve(db):
    """The curve describes cumulative totals; a level-internal bar would fail
    it for no reason."""
    from app.japanese_import import preview_save

    preview = preview_save(
        db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000)
    )
    assert preview.curve_problems == []


def test_the_preview_announces_a_boss(db):
    from app.japanese_import import preview_save

    _seed_v2_baseline(db)
    preview = preview_save(db, save_text(**BOSS_SAVE))

    assert preview.rank_boss.reward_name == "旅立ちの証"
    assert preview.rank_reward_already_held is False


def test_the_preview_writes_nothing(db):
    from app.japanese_import import preview_save

    _seed_v2_baseline(db)
    before = db.query(JapaneseSaveImport).count()
    preview_save(db, save_text(**BOSS_SAVE))

    assert db.query(JapaneseSaveImport).count() == before
    assert db.query(JapaneseRankReward).count() == 0


# ---------------------------------------------------------------------------
# The bounded progress bar
# ---------------------------------------------------------------------------

def test_a_cumulative_bar_never_exceeds_full():
    """The coach's bar may legitimately read past its threshold for a moment;
    a bar drawn at 104% would spill out of its container."""
    from app.japanese_levels import progress_view

    view = progress_view(2, 1038, 1000, cumulative=True)
    assert view.percent == 100


def test_a_legacy_bar_never_exceeds_full():
    from app.japanese_levels import progress_view

    assert progress_view(2, 1038, 1000, cumulative=False).percent == 100


def test_a_zero_cap_does_not_divide_by_zero():
    from app.japanese_levels import progress_view

    assert progress_view(2, 5, 0, cumulative=False).percent == 0


def test_the_bar_reflects_the_position_inside_the_level():
    from app.japanese_levels import progress_view

    # Level 3 spans 1000..2100. 1550 is halfway.
    assert progress_view(3, 1550, 2100, cumulative=True).percent == 50


def test_max_level_shows_a_full_bar():
    from app.japanese_levels import progress_view

    view = progress_view(30, 70000, 0, cumulative=True)
    assert view.at_max is True
    assert view.percent == 100


# ---------------------------------------------------------------------------
# What the reward uniqueness rests on
# ---------------------------------------------------------------------------

def test_the_reward_uniqueness_is_on_the_boss_id_alone():
    """Correct only because this application has exactly one user.

    With real accounts, boss 01 could be claimed once across the whole
    installation — a genuine data model bug. The two tests below pin the
    assumption that makes the bare key sound, so adding accounts breaks here
    and points straight at the constraint that has to become composite.
    """
    from app.models import JapaneseRankReward

    boss_column = JapaneseRankReward.__table__.c.boss_id
    assert boss_column.unique is True

    owner_like = [
        c.name
        for c in JapaneseRankReward.__table__.columns
        if c.name in {"user_id", "owner_id", "account_id", "profile_id", "hero_id"}
    ]
    assert not owner_like, (
        f"{owner_like} exists, so uniqueness must be composite over it and boss_id"
    )


def test_the_schema_has_no_user_concept_at_all():
    """The single-user assumption, checked across the whole model.

    `app.auth` protects access with one password rather than separating
    tenants, and no table carries an owner. If that ever changes, every
    'unique per installation' key in the project needs revisiting — starting
    with japanese_rank_rewards.boss_id.
    """
    from app.models import Base

    offenders = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name in {"user_id", "owner_id", "account_id", "tenant_id"}:
                offenders.append(f"{table.name}.{column.name}")

    assert not offenders, (
        "The schema gained an owner key: " + ", ".join(offenders) + ". "
        "japanese_rank_rewards.boss_id must become unique per owner."
    )
    assert "users" not in Base.metadata.tables


def test_two_bosses_do_not_collide_with_each_other(db):
    """The constraint separates bosses, which is what it is for."""
    from app.models import JapaneseRankReward

    _seed_v2_baseline(db)
    import_save(db, save_text(**BOSS_SAVE))
    import_save(
        db,
        save_text(level=10, rank="熟練者", xp=10850, cap=12600, day="2026-09-01",
                  boss="jp-rank-boss-02", status="bestanden",
                  reward_id="jp-rank-reward-02", reward_name="修行の証"),
    )
    assert db.query(JapaneseRankReward).count() == 2


def test_the_database_refuses_a_duplicate_boss_row(db):
    """Not just the preceding query — the index itself. Two concurrent imports
    that both pass the check still cannot both insert."""
    from sqlalchemy.exc import IntegrityError

    from app.models import JapaneseRankReward

    for _ in range(2):
        db.add(
            JapaneseRankReward(
                boss_id="jp-rank-boss-01",
                reward_id="jp-rank-reward-01",
                reward_name="旅立ちの証",
                boss_title="昇格試験 I – 旅立ちの試練",
                source_level=5,
                tier=1,
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# Under version 2 the cumulative total is canonical
# ---------------------------------------------------------------------------

def test_the_total_decides_level_rank_and_cap():
    from app.japanese_levels import canonical_state

    state = canonical_state(1038)
    assert (state.level, state.rank, state.next_threshold) == (3, "修行者", 2100)


@pytest.mark.parametrize(
    "total,level,rank,cap",
    [
        (0, 2, "見習い", 1000),
        (999, 2, "見習い", 1000),
        (1000, 3, "修行者", 2100),
        (2100, 4, "探究者", 3300),
        (65800, 30, "言霊の覇者", None),
        (99999, 30, "言霊の覇者", None),
    ],
)
def test_the_canonical_state_follows_the_curve(total, level, rank, cap):
    from app.japanese_levels import canonical_state

    state = canonical_state(total)
    assert (state.level, state.rank, state.next_threshold) == (level, rank, cap)


def test_a_contradictory_claim_is_stored_in_its_corrected_form(db):
    """The row must not disagree with itself, and the next import must not be
    handed the wrong level."""
    _seed_v2_baseline(db, xp=1000)
    result = import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))
    row = result.created

    assert row.source_character_level == 3
    assert row.source_character_rank == "修行者"
    assert row.source_level_xp_cap == 2100
    assert row.source_level_xp == 1038


def test_correcting_the_claim_does_not_block_the_xp(db):
    _seed_v2_baseline(db, xp=1000)
    result = import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))

    assert result.xp_awarded == 38
    assert result.created.warning_text


def test_the_disagreement_is_still_reported(db):
    """Corrected, not hidden."""
    _seed_v2_baseline(db, xp=1000)
    result = import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))

    assert "passt nicht zu 1038 Gesamt-XP" in result.created.warning_text


def test_the_raw_save_keeps_what_was_actually_written(db):
    """The correction is a stored interpretation, never a rewrite of the source."""
    _seed_v2_baseline(db, xp=1000)
    result = import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))

    assert "Lv 2 (見習い) | 1038 / 1000 XP" in result.created.raw_save


def test_a_consistent_version_two_save_is_stored_unchanged(db):
    """Correction only ever moves a row onto the curve; a row already on it
    must come out byte-for-byte the same."""
    _seed_v2_baseline(db, xp=1000)
    result = import_save(db, save_text(level=3, rank="修行者", xp=1038, cap=2100))
    row = result.created

    assert (row.source_character_level, row.source_character_rank, row.source_level_xp_cap) == (
        3, "修行者", 2100,
    )
    assert row.warning_text is None


def test_a_legacy_save_is_never_corrected(db):
    """Version 1 counts inside a level, so the curve says nothing about it and
    its claim is the only truth there is."""
    result = import_save(
        db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000)
    )
    row = result.created

    assert row.source_character_level == 2
    assert row.source_character_rank == "見習い"
    assert row.source_level_xp_cap == 1000


def test_the_next_import_measures_from_the_corrected_row(db):
    """The point of correcting: a contradictory row would otherwise hand the
    wrong level to every later delta."""
    _seed_v2_baseline(db, xp=1000)
    import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))

    later = import_save(
        db, save_text(level=3, rank="修行者", xp=1100, cap=2100, day="2026-08-20")
    )
    assert later.xp_awarded == 62          # 1100 - 1038, measured from the total
    assert later.created.warning_text is None


def test_max_level_adopts_no_cap_at_all(db):
    """Level 30 has no next threshold, so there is no canonical cap to store.
    Taking the claim instead would persist the very contradiction the
    correction exists to remove, so 0 records "none".

    Superseded the earlier expectation that the coach's cap was kept: an
    adversarial pass showed that let a self-contradicting row through.
    """
    _seed_v2_baseline(db, xp=65800)
    result = import_save(
        db, save_text(level=30, rank="言霊の覇者", xp=66000, cap=65800, day="2026-08-20")
    )
    assert result.created.source_character_level == 30
    assert result.created.source_level_xp_cap == 0


def test_the_stored_row_always_agrees_with_the_curve(db):
    """The invariant itself, over a spread of contradictory claims."""
    from app.japanese_levels import level_for_total_xp

    _seed_v2_baseline(db, xp=0)
    for n, (level, rank, xp, cap) in enumerate(
        [(2, "見習い", 1038, 1000), (9, "使い手", 2200, 10800), (3, "修行者", 5000, 2100)]
    ):
        result = import_save(
            db, save_text(level=level, rank=rank, xp=xp, cap=cap, day=f"2026-09-0{n + 1}")
        )
        row = result.created
        assert row.source_character_level == level_for_total_xp(row.source_level_xp)


# ---------------------------------------------------------------------------
# Gaps an adversarial pass found in the first version of the correction
# ---------------------------------------------------------------------------

def test_level_thirty_has_no_canonical_cap_so_none_is_adopted(db):
    """At the ceiling there is no next threshold, so taking the claim would
    persist exactly the contradiction the correction removes. The coach spec
    leaves the right-hand side undefined there, so it is not warned about —
    it is simply not adopted as fact."""
    _seed_v2_baseline(db, xp=65800)
    result = import_save(
        db, save_text(level=30, rank="言霊の覇者", xp=66000, cap=99999, day="2026-08-20")
    )
    assert result.created.source_character_level == 30
    assert result.created.source_level_xp_cap == 0


def test_level_thirty_still_pays_normally(db):
    _seed_v2_baseline(db, xp=65800)
    result = import_save(
        db, save_text(level=30, rank="言霊の覇者", xp=66000, cap=99999, day="2026-08-20")
    )
    assert result.xp_awarded == 200


@pytest.mark.parametrize(
    "extra,label",
    [
        (("Session-Modus: GENKI", "Session-Abschluss: vollständig"), "deterministic"),
        (("Session-Modus: unsinn", "Session-Abschluss: vollständig"), "unreadable"),
    ],
)
def test_a_session_line_does_not_smuggle_a_claim_past_the_curve(db, extra, label):
    """Those paths return before the cumulative branch, but the import corrects
    the level anyway — so validating only inside that branch meant a silent
    correction, which is the hiding this check exists to prevent."""
    _seed_v2_baseline(db, xp=1000)
    result = import_save(
        db, save_text(level=2, rank="見習い", xp=1038, cap=1000, extra_lines=extra)
    )

    assert result.created.source_character_level == 3
    assert "passt nicht zu 1038 Gesamt-XP" in (result.created.warning_text or "")


def test_the_first_import_is_also_checked_against_the_curve(db):
    """The baseline path returns early too."""
    result = import_save(db, save_text(level=2, rank="見習い", xp=1038, cap=1000))
    assert "passt nicht zu 1038 Gesamt-XP" in (result.created.warning_text or "")


def test_a_backdated_save_is_also_checked_against_the_curve(db):
    _seed_v2_baseline(db, xp=1000, day="2026-08-18")
    result = import_save(
        db, save_text(level=2, rank="見習い", xp=1038, cap=1000, day="2026-08-01")
    )
    assert "passt nicht zu 1038 Gesamt-XP" in (result.created.warning_text or "")


def test_a_legacy_save_is_still_never_checked(db):
    result = import_save(
        db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000)
    )
    assert result.created.warning_text is None or "Gesamt-XP" not in result.created.warning_text


# ---------------------------------------------------------------------------
# A cumulative counter is measured from its high-water mark
# ---------------------------------------------------------------------------

def test_a_total_that_fell_does_not_become_the_measuring_point(db):
    """Otherwise the recovery pays the range between the two all over again."""
    _seed_v2_baseline(db, xp=2000)

    dropped = import_save(db, save_text(xp=1500, cap=2100, day="2026-08-19"))
    recovered = import_save(db, save_text(xp=2000, cap=2100, day="2026-08-20"))

    assert dropped.xp_awarded == 0
    assert recovered.xp_awarded == 0


def test_progress_past_the_high_water_mark_still_pays(db):
    _seed_v2_baseline(db, xp=2000)
    import_save(db, save_text(xp=1500, cap=2100, day="2026-08-19"))
    import_save(db, save_text(xp=2000, cap=2100, day="2026-08-20"))

    ahead = import_save(db, save_text(xp=2050, cap=2100, day="2026-08-21"))
    assert ahead.xp_awarded == 50


def test_the_dropped_snapshot_is_still_kept(db):
    """It is a record of what the coach reported, warning and all."""
    _seed_v2_baseline(db, xp=2000)
    result = import_save(db, save_text(xp=1500, cap=2100, day="2026-08-19"))

    assert result.created is not None
    assert result.created.source_level_xp == 1500
    assert "gesunken" in (result.created.warning_text or "")


def test_the_high_water_mark_ignores_legacy_rows(db):
    """Legacy totals are level-internal and are not on the same scale."""
    import_save(db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000))
    result = import_save(
        db, save_text(level=3, rank="修行者", xp=1038, cap=2100, day="2026-08-20")
    )
    assert result.xp_awarded == 330      # the bridge, not a high-water comparison


def test_a_normal_ascending_chain_is_unaffected(db):
    _seed_v2_baseline(db, xp=1000)
    for n, total in enumerate((1038, 1100, 1250), start=1):
        result = import_save(db, save_text(xp=total, cap=2100, day=f"2026-08-2{n}"))
    assert result.created.source_level_xp == 1250
    assert db.query(HeroProfile).one().total_xp == 250


def test_a_version_one_save_after_a_version_two_one_invents_nothing(db):
    """The two scales are not comparable: the previous value is a cumulative
    total, the current bar counts inside its level. Subtracting one from the
    other produced +800 out of nothing."""
    _seed_v2_baseline(db, xp=1500)
    result = import_save(
        db, save_text(version=None, level=4, rank="探究者", xp=200, cap=3300, day="2026-08-20")
    )

    assert result.xp_awarded == 0
    assert "Version 1" in result.created.warning_text
    assert "kumulativen Gesamtstand" in result.created.warning_text


def test_that_refusal_does_not_touch_a_normal_legacy_chain(db):
    import_save(db, save_text(version=None, level=2, rank="見習い", xp=708, cap=1000))
    result = import_save(
        db, save_text(version=None, level=2, rank="見習い", xp=808, cap=1000, day="2026-08-20")
    )
    assert result.xp_awarded == 100
    assert result.created.warning_text is None


def test_a_zero_cap_at_max_level_no_longer_misleads(db):
    """The stored cap of 0 used to surface as "Unplausible XP-Obergrenze",
    blaming the data instead of the schema mismatch."""
    _seed_v2_baseline(db, xp=65800)
    import_save(
        db, save_text(level=30, rank="言霊の覇者", xp=66000, cap=99999, day="2026-08-20")
    )
    result = import_save(
        db, save_text(version=None, level=30, rank="言霊の覇者", xp=120, cap=3000, day="2026-08-21")
    )
    assert "Version 1" in result.created.warning_text
    assert "Unplausible" not in result.created.warning_text
