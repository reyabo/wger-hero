"""SAVE version 2: cumulative XP, the version bridge, and rank boss rewards.

The bug this fixes came from the coach switching to cumulative totals while
wger-hero still read the bar as level-internal XP. Once a total passes the
current level's threshold, the old code called it implausible and paid nothing:

    Charakter: Lv 2 (見習い) | 1038 / 1000 XP
    → "Der Levelbalken liegt über der Obergrenze. Es wird kein XP vergeben."

So the first thing tested here is that a version-2 SAVE is read with version-2
semantics, and the second is that a version-1 SAVE is not.

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

def test_a_total_above_the_level_cap_no_longer_blocks_the_reward():
    """The exact symptom: a cumulative bar reading past its own threshold."""
    parsed = parse_save(save_text(level=2, rank="見習い", xp=1038, cap=1000))
    result = calculate_delta(parsed, legacy_previous())

    assert result.xp_delta == 330
    assert result.classification != "warning" or result.xp_delta > 0


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


def test_a_level_up_does_not_inflate_the_delta():
    """The old level-change formula would give (2100-2090)+2120 = 2130."""
    result = calculate_delta(
        parse_save(save_text(level=4, rank="探究者", xp=2120, cap=3300)),
        v2_previous(2090),
    )
    assert result.xp_delta == 30


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
