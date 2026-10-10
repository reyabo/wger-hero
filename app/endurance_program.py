"""The optional goal programme "Kardiovaskuläre Ausdauer".

Models one specific training plan: one endurance session on Tuesday and one at
the weekend. The two weekly quests are exactly those two slots, which is what
makes the existing goal engine do the right thing without a line of special
casing — a week counts as successful when every weekly quest of the goal was
rewarded, so "Tuesday *and* the weekend" falls out of the data.

Everything else the goal carries — the monthly volume quest and the five
milestones — is deliberately *not* weekly, so it cannot affect that judgement.

Nothing here runs on startup or during a migration. It is created only when the
user asks for it, and asking twice changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Goal, Quest
from app.quests import create_quest, serialize_allowed_weekdays

logger = logging.getLogger(__name__)

GOAL_SLUG = "kardiovaskulaere-ausdauer"
GOAL_TITLE = "Kardiovaskuläre Ausdauer"
GOAL_SHORT = "Ausdauer"
GOAL_DESCRIPTION = (
    "Eine belastbare Ausdauerbasis durch regelmäßige, in FitTrackee "
    "dokumentierte Ausdauereinheiten aufbauen.\n\n"
    "Hero bewertet Beständigkeit und Trainingszeit. Geschwindigkeit, "
    "Herzfrequenz und Distanz werden nicht für XP optimiert."
)

# ISO weekdays, the project's one convention (1 = Mon … 7 = Sun).
TUESDAY = [2]
WEEKEND = [6, 7]

CREATE = "create"
REUSE = "reuse"
EXTEND = "extend"
SKIP = "skip"
CONFLICT = "conflict"

ACTION_LABELS = {
    CREATE: "wird angelegt",
    REUSE: "vorhanden, wird verwendet",
    EXTEND: "vorhanden, wird dem Ziel zugeordnet",
    SKIP: "vorhanden, bleibt unverändert",
    CONFLICT: "gehört zu einem anderen Ziel, bleibt unberührt",
}


@dataclass(frozen=True)
class QuestSpec:
    """One quest of the programme, as data rather than as code.

    ``allowed_weekdays`` is the only new parameter and it is a validated list of
    ISO weekday numbers — not an expression, not JSON, not a query fragment.
    """

    title: str
    description: str
    quest_type: str
    period: str
    target_value: int
    xp_reward: int
    stat_xp: int
    repeatable: bool = False
    is_milestone: bool = False
    allowed_weekdays: Optional[list[int]] = None
    kind: str = "Quest"


# The two planned slots. These two, and only these two, are weekly — so the
# goal's successful week means "Tuesday and a weekend day", nothing else.
WEEKLY_QUESTS = (
    QuestSpec(
        title="Dienstagsrunde",
        description=(
            "Absolviere am Dienstag mindestens eine qualifizierende, über "
            "FitTrackee synchronisierte Ausdauereinheit."
        ),
        quest_type="fittrackee_workout_count",
        period="weekly",
        target_value=1,
        xp_reward=75,
        stat_xp=75,
        repeatable=True,
        allowed_weekdays=TUESDAY,
    ),
    QuestSpec(
        title="Wochenendrunde",
        description=(
            "Absolviere am Samstag oder Sonntag mindestens eine qualifizierende, "
            "über FitTrackee synchronisierte Ausdauereinheit. Ein Wochenendtag "
            "genügt."
        ),
        quest_type="fittrackee_workout_count",
        period="weekly",
        target_value=1,
        xp_reward=100,
        stat_xp=100,
        repeatable=True,
        allowed_weekdays=WEEKEND,
    ),
)

# Monthly, therefore outside the weekly judgement by construction.
MONTHLY_QUESTS = (
    QuestSpec(
        title="Vier Stunden Basis",
        description=(
            "Sammle im Kalendermonat mindestens vier Stunden qualifizierende "
            "Ausdauerzeit."
        ),
        quest_type="fittrackee_duration_minutes",
        period="monthly",
        target_value=240,
        xp_reward=150,
        stat_xp=150,
        repeatable=True,
    ),
)

# The names are motivating labels. The completion condition is only ever the
# number of qualifying workouts or minutes — no calendar week is derived.
MILESTONES = (
    QuestSpec(
        title="Der erste Schritt",
        description="Die erste qualifizierende Ausdauereinheit nach der Baseline.",
        quest_type="fittrackee_workout_count",
        period="once",
        target_value=1,
        xp_reward=75,
        stat_xp=75,
        is_milestone=True,
        kind="Meilenstein",
    ),
    QuestSpec(
        title="Fünf Wochen Ausdauer",
        description=(
            "Zehn qualifizierende Ausdauereinheiten. Bei zwei Einheiten pro "
            "Woche entspricht das etwa fünf Trainingswochen; gezählt werden "
            "die Einheiten, nicht die Wochen."
        ),
        quest_type="fittrackee_workout_count",
        period="once",
        target_value=10,
        xp_reward=125,
        stat_xp=125,
        is_milestone=True,
        kind="Meilenstein",
    ),
    QuestSpec(
        title="Zehn Stunden Ausdauer",
        description="600 qualifizierende Ausdauerminuten insgesamt.",
        quest_type="fittrackee_duration_minutes",
        period="once",
        target_value=600,
        xp_reward=175,
        stat_xp=175,
        is_milestone=True,
        kind="Meilenstein",
    ),
    QuestSpec(
        title="Fünfzehn Wochen Ausdauer",
        description=(
            "Dreißig qualifizierende Ausdauereinheiten. Auch hier zählen die "
            "Einheiten, nicht die Kalenderwochen."
        ),
        quest_type="fittrackee_workout_count",
        period="once",
        target_value=30,
        xp_reward=225,
        stat_xp=225,
        is_milestone=True,
        kind="Meilenstein",
    ),
    QuestSpec(
        title="Dreißig Stunden Ausdauer",
        description="1800 qualifizierende Ausdauerminuten insgesamt.",
        quest_type="fittrackee_duration_minutes",
        period="once",
        target_value=1800,
        xp_reward=300,
        stat_xp=300,
        is_milestone=True,
        kind="Meilenstein",
    ),
)

ALL_QUESTS = WEEKLY_QUESTS + MONTHLY_QUESTS + MILESTONES


@dataclass
class PlanItem:
    kind: str
    title: str
    action: str
    detail: str = ""

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)


@dataclass
class EndurancePlan:
    applied: bool = False
    items: list[PlanItem] = field(default_factory=list)
    created_goal: Optional[int] = None
    created_quests: list[int] = field(default_factory=list)

    def add(self, kind: str, title: str, action: str, detail: str = "") -> None:
        self.items.append(PlanItem(kind=kind, title=title, action=action, detail=detail))

    @property
    def creates_anything(self) -> bool:
        return any(item.action == CREATE for item in self.items)


class EnduranceProgramError(RuntimeError):
    """The programme could not be created. Nothing was saved."""


def _goal(db: Session, plan: EndurancePlan, apply: bool) -> Optional[Goal]:
    existing = db.query(Goal).filter(Goal.slug == GOAL_SLUG).first()
    if existing is not None:
        plan.add("Ziel", GOAL_TITLE, REUSE, "Vorhanden; Titel und Text bleiben.")
        return existing

    plan.add("Ziel", GOAL_TITLE, CREATE, "Neues Ziel mit Status „aktiv“.")
    if not apply:
        return None

    goal = Goal(
        slug=GOAL_SLUG,
        title=GOAL_TITLE,
        description=GOAL_DESCRIPTION,
        short_label=GOAL_SHORT,
        status="active",
    )
    db.add(goal)
    db.flush()
    plan.created_goal = goal.id
    return goal


def _matches_template(quest: Quest, spec: QuestSpec) -> bool:
    """Whether a quest still looks exactly like the template that made it.

    Only then may a controlled update touch it. Anything the user changed —
    target, reward, period, weekday set — makes this false, and the row is left
    alone rather than reset.
    """
    return (
        (quest.quest_type or "") == spec.quest_type
        and (quest.period or "") == spec.period
        and int(quest.target_value or 0) == spec.target_value
        and int(quest.xp_reward or 0) == spec.xp_reward
        and (quest.allowed_weekdays or None)
        == serialize_allowed_weekdays(spec.allowed_weekdays)
    )


def _quest(db: Session, plan: EndurancePlan, goal: Optional[Goal], spec: QuestSpec,
           apply: bool) -> None:
    existing = db.query(Quest).filter(Quest.title == spec.title).first()

    if existing is not None:
        if existing.goal_id is not None and (goal is None or existing.goal_id != goal.id):
            plan.add(spec.kind, spec.title, CONFLICT)
            return
        if existing.goal_id is None:
            plan.add(spec.kind, spec.title, EXTEND,
                     "Wird dem Ziel zugeordnet, sonst unverändert.")
            if apply and goal is not None:
                existing.goal_id = goal.id
            return
        if _matches_template(existing, spec):
            plan.add(spec.kind, spec.title, SKIP, "Entspricht der Vorlage.")
        else:
            plan.add(spec.kind, spec.title, SKIP,
                     "Wurde angepasst und bleibt so. Es wird nichts zurückgesetzt.")
        return

    plan.add(spec.kind, spec.title, CREATE, _detail(spec))
    if not apply or goal is None:
        return

    quest = create_quest(
        db,
        title=spec.title,
        description=spec.description,
        quest_type=spec.quest_type,
        period=spec.period,
        target_value=spec.target_value,
        xp_reward=spec.xp_reward,
        stat_rewards={"endurance": spec.stat_xp},
        repeatable=spec.repeatable,
        goal_id=goal.id,
        allowed_weekdays=spec.allowed_weekdays,
    )
    quest.is_milestone = spec.is_milestone
    plan.created_quests.append(quest.id)


def _detail(spec: QuestSpec) -> str:
    from app.habits import WEEKDAY_SHORT

    if spec.quest_type == "fittrackee_duration_minutes":
        what = f"{spec.target_value} qualifizierende Ausdauerminuten"
    else:
        unit = "Einheit" if spec.target_value == 1 else "Einheiten"
        what = f"{spec.target_value} qualifizierende {unit}"
    if spec.allowed_weekdays:
        days = " oder ".join(WEEKDAY_SHORT[d] for d in spec.allowed_weekdays)
        what += f", nur {days}"
    return f"{what}. Belohnung {spec.xp_reward} XP und {spec.stat_xp} Ausdauer-XP."


def _walk(db: Session, apply: bool) -> EndurancePlan:
    plan = EndurancePlan(applied=apply)
    goal = _goal(db, plan, apply)
    for spec in ALL_QUESTS:
        _quest(db, plan, goal, spec, apply)
    return plan


def plan_endurance_program(db: Session) -> EndurancePlan:
    """What an activation would do. Writes nothing."""
    plan = _walk(db, apply=False)
    db.rollback()
    return plan


def apply_endurance_program(db: Session) -> EndurancePlan:
    """Create the programme, all or nothing.

    Never deletes anything and never touches XP history: the worst case is that
    an aborted run removes only the rows it created in this very call.
    """
    plan = EndurancePlan(applied=True)
    try:
        goal = _goal(db, plan, True)
        for spec in ALL_QUESTS:
            _quest(db, plan, goal, spec, True)
        db.commit()
        return plan
    except Exception as exc:  # noqa: BLE001 — re-raised sanitized
        logger.exception("Endurance programme failed; removing what it created")
        try:
            db.rollback()
            if plan.created_quests:
                db.query(Quest).filter(Quest.id.in_(plan.created_quests)).delete(
                    synchronize_session=False
                )
            if plan.created_goal is not None:
                db.query(Goal).filter(Goal.id == plan.created_goal).delete(
                    synchronize_session=False
                )
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Cleanup after a failed endurance programme also failed")
        raise EnduranceProgramError(
            "Das Ausdauerziel konnte nicht angelegt werden. Es wurde nichts gespeichert."
        ) from exc
