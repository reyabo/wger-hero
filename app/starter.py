"""
The starter campaign: three prepared goals a fresh or existing install can adopt.

Nothing here runs by itself. The campaign is applied only when the user asks for
it, either from the settings page or with

    python -m app.seed_programs starter [--dry-run]

Both paths call the same two functions, so a preview can never disagree with
what an activation would actually do: ``plan_starter`` walks the definition
read-only, ``apply_starter`` walks the very same code with writing enabled and
commits once at the end.

What it deliberately never does: award XP, create habit completions, create
quest completions, invent pause intervals, import a Japanese SAVE, contact wger,
rename or delete anything the user owns, or overwrite a field the user has
edited. It only adds what is missing and reports what it left alone.

Existing rows are matched by **exact title** plus the goal they belong to — not
by a fuzzy substring. A row that already exists with a different owner is
reported as a conflict and left untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.goals import STATUS_ACTIVE, create_goal, get_by_slug
from app.habits import create_habit, scheduled_weekdays, set_weekdays
from app.models import Goal, Habit, Quest
from app.quests import create_quest

logger = logging.getLogger(__name__)

STARTER_KEY = "starter"

# What happened, or would happen, to one entry.
CREATE = "create"       # did not exist, will be added
REUSE = "reuse"         # already exists and is used as it is
EXTEND = "extend"       # exists, a missing link or plan is added to it
SKIP = "skip"           # exists and is already complete — nothing to do
CONFLICT = "conflict"   # exists but belongs elsewhere; left untouched

ACTION_LABELS = {
    CREATE: "wird neu angelegt",
    REUSE: "wird wiederverwendet",
    EXTEND: "wird ergänzt",
    SKIP: "keine Änderung",
    CONFLICT: "Konflikt — bleibt unverändert",
}

# A break is never punished, so the safety note can stay factual.
SAFETY_NOTE = (
    "Bei anhaltender Spannung oder Schmerzen die Einheit pausieren. "
    "Eine Pause kostet kein XP und unterbricht keine Serie. "
    "Bei anhaltenden Beschwerden ärztlich abklären lassen."
)


class StarterError(RuntimeError):
    """Something went wrong while applying — the transaction is rolled back."""


@dataclass
class PlanItem:
    kind: str            # "Ziel" | "Gewohnheit" | "Quest" | "Meilenstein"
    name: str
    action: str
    detail: str = ""
    goal: Optional[str] = None

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def is_conflict(self) -> bool:
        return self.action == CONFLICT


@dataclass
class _Created:
    """Exactly what this run added, so a failure can be undone precisely.

    The create_* services of habits.py, goals.py and quests.py each commit on
    their own — that is what makes them reusable everywhere else, and it is why
    a SAVEPOINT cannot wrap them. To still keep the promise "no half-created
    campaign", the ids of the rows *this run* inserted are recorded and removed
    again if anything fails afterwards. Nothing the user owns is ever touched
    by that cleanup: only rows that did not exist a moment earlier.
    """

    goals: list[int] = field(default_factory=list)
    habits: list[int] = field(default_factory=list)
    quests: list[int] = field(default_factory=list)


@dataclass
class StarterPlan:
    items: list[PlanItem] = field(default_factory=list)
    applied: bool = False
    created: "_Created" = field(default_factory=lambda: _Created())

    def add(self, *args, **kw) -> PlanItem:
        item = PlanItem(*args, **kw)
        self.items.append(item)
        return item

    def of_action(self, action: str) -> list[PlanItem]:
        return [i for i in self.items if i.action == action]

    @property
    def conflicts(self) -> list[PlanItem]:
        return self.of_action(CONFLICT)

    @property
    def changes(self) -> int:
        """How many entries this run would actually touch."""
        return len(self.of_action(CREATE)) + len(self.of_action(EXTEND))

    def summary(self) -> dict[str, int]:
        return {action: len(self.of_action(action)) for action in ACTION_LABELS}


# ---------------------------------------------------------------------------
# Campaign definition
# ---------------------------------------------------------------------------

# ISO weekdays: 1 = Monday … 7 = Sunday
JAPANESE_WEEKDAYS = [1, 2, 3, 4, 5]

# The five neutral routines of the existing CONTROL program and the day each is
# planned for. Friday and Sunday stay deliberately free.
CONTROL_SCHEDULE = {
    "Kontrolltraining – Kraft": 1,
    "Atem & Wahrnehmung": 2,
    "Kontrollsession": 3,
    "Lösen & Entspannen": 4,
    "Lange Kontrollsession": 6,
}


# ---------------------------------------------------------------------------
# Small helpers — each one either only looks, or only writes when `apply`
# ---------------------------------------------------------------------------

def _goal(db: Session, plan: StarterPlan, *, slug: str, title: str,
          description: str, short_label: Optional[str], apply: bool) -> Optional[Goal]:
    existing = get_by_slug(db, slug)
    if existing is not None:
        # An existing goal is used as it is. Its title, description and status
        # belong to the user now, not to this definition.
        plan.add("Ziel", title, REUSE, f"Slug „{slug}“ ist bereits vorhanden.")
        return existing

    plan.add("Ziel", title, CREATE, f"Slug „{slug}“, Status aktiv.")
    if not apply:
        return None
    goal = create_goal(
        db,
        title=title,
        description=description,
        short_label=short_label,
        status=STATUS_ACTIVE,
        slug=slug,
    )
    plan.created.goals.append(goal.id)
    return goal


def _habit(db: Session, plan: StarterPlan, *, title: str, description: str,
           goal: Optional[Goal], goal_title: str, weekdays: list[int],
           recurrence: str, target_count: int, category: str,
           duration_size: str, effort: str, apply: bool) -> Optional[Habit]:
    existing = db.query(Habit).filter(Habit.title == title).first()

    if existing is not None:
        if existing.goal_id is not None and (goal is None or existing.goal_id != goal.id):
            plan.add("Gewohnheit", title, CONFLICT, goal=goal_title,
                     detail="Gehört bereits zu einem anderen Ziel.")
            return existing

        missing_plan = weekdays and scheduled_weekdays(db, existing) != weekdays
        missing_link = existing.goal_id is None
        if not missing_plan and not missing_link:
            plan.add("Gewohnheit", title, SKIP, goal=goal_title,
                     detail="Vorhanden, bereits zugeordnet und geplant.")
            return existing

        details = []
        if missing_link:
            details.append("Zuordnung zum Ziel")
        if missing_plan:
            details.append("Wochenplanung " + _weekday_text(weekdays))
        plan.add("Gewohnheit", title, EXTEND, goal=goal_title,
                 detail="Ergänzt: " + ", ".join(details) + ". Titel und Text bleiben.")
        if apply:
            if missing_link and goal is not None:
                existing.goal_id = goal.id
            if missing_plan:
                set_weekdays(db, existing, weekdays)
        return existing

    plan.add("Gewohnheit", title, CREATE, goal=goal_title,
             detail=_weekday_text(weekdays) if weekdays else "ohne feste Wochentage")
    if not apply:
        return None

    habit = create_habit(
        db,
        title=title,
        description=description,
        recurrence=recurrence,
        target_count=target_count,
        category=category,
        duration_size=duration_size,
        effort=effort,
    )
    plan.created.habits.append(habit.id)
    if goal is not None:
        habit.goal_id = goal.id
    if weekdays:
        set_weekdays(db, habit, weekdays)
    return habit


def _quest(db: Session, plan: StarterPlan, *, title: str, description: str,
           goal: Optional[Goal], goal_title: str, quest_type: str, period: str,
           target_value: int, repeatable: bool, is_milestone: bool,
           habit: Optional[Habit] = None, reuse_slug: Optional[str] = None,
           kind: str = "Quest", apply: bool = False) -> Optional[Quest]:
    """Create, reuse or extend one quest.

    `reuse_slug` names an existing quest that already does this job — the seeded
    "week-warrior" is exactly "three workouts per week". It is linked to the
    goal rather than duplicated, and neither its title nor its reward is
    touched.
    """
    if reuse_slug:
        seeded = db.query(Quest).filter(Quest.slug == reuse_slug).first()
        if seeded is not None:
            if seeded.goal_id is not None and (goal is None or seeded.goal_id != goal.id):
                plan.add(kind, seeded.title, CONFLICT, goal=goal_title,
                         detail="Erfüllt dieselbe Aufgabe, gehört aber zu einem "
                                "anderen Ziel. Es wird keine zweite Quest angelegt.")
                return seeded
            if seeded.goal_id is None:
                plan.add(kind, seeded.title, EXTEND, goal=goal_title,
                         detail=f"Erfüllt „{title}“ bereits; wird dem Ziel zugeordnet. "
                                "Titel und Belohnung bleiben unverändert.")
                if apply and goal is not None:
                    seeded.goal_id = goal.id
            else:
                plan.add(kind, seeded.title, REUSE, goal=goal_title,
                         detail=f"Erfüllt „{title}“ bereits und ist zugeordnet.")
            return seeded

    existing = db.query(Quest).filter(Quest.title == title).first()
    if existing is not None:
        if existing.goal_id is not None and (goal is None or existing.goal_id != goal.id):
            plan.add(kind, title, CONFLICT, goal=goal_title,
                     detail="Gehört bereits zu einem anderen Ziel.")
            return existing
        if existing.goal_id is None:
            plan.add(kind, title, EXTEND, goal=goal_title,
                     detail="Wird dem Ziel zugeordnet, sonst unverändert.")
            if apply and goal is not None:
                existing.goal_id = goal.id
            return existing
        plan.add(kind, title, SKIP, goal=goal_title, detail="Vorhanden und zugeordnet.")
        return existing

    plan.add(kind, title, CREATE, goal=goal_title, detail=_quest_detail(quest_type, target_value))
    if not apply:
        return None

    quest = create_quest(
        db,
        title=title,
        description=description,
        quest_type=quest_type,
        period=period,
        target_value=target_value,
        repeatable=repeatable,
        category="technique_skill",
        duration_size="normal",
        effort="normal",
    )
    plan.created.quests.append(quest.id)
    if goal is not None:
        quest.goal_id = goal.id
    quest.is_milestone = is_milestone
    if habit is not None:
        quest.habit_id = habit.id
    db.commit()
    return quest


def _weekday_text(weekdays: list[int]) -> str:
    from app.habits import WEEKDAY_SHORT

    return "Wochentage: " + ", ".join(WEEKDAY_SHORT[d] for d in weekdays)


def _quest_detail(quest_type: str, target: int) -> str:
    sources = {
        "workout_count": "gezählte wger-Workouts",
        "habit_count": "Abschlüsse der verknüpften Gewohnheit",
        "japanese_session_count": "bestätigte Japanisch-Sessions",
        "manual": "manuell bestätigt",
    }
    return f"Ziel {target}, Quelle: {sources.get(quest_type, quest_type)}."


# ---------------------------------------------------------------------------
# The three goals
# ---------------------------------------------------------------------------

def _strength(db: Session, plan: StarterPlan, apply: bool) -> None:
    goal = _goal(
        db, plan,
        slug="kraftpfad",
        title="Kraftpfad",
        description="Regelmäßiges Krafttraining, gezählt aus den synchronisierten "
                    "wger-Einheiten.",
        short_label="Kraft",
        apply=apply,
    )

    _quest(
        db, plan,
        title="Dreifachschlag",
        description="Drei gewertete wger-Workouts in einer Kalenderwoche.",
        goal=goal, goal_title="Kraftpfad",
        quest_type="workout_count", period="weekly", target_value=3,
        repeatable=True, is_milestone=False,
        reuse_slug="week-warrior",
        apply=apply,
    )

    for title, target, description in (
        ("Erstes gewertetes Workout", 1,
         "Das erste aus wger übernommene Training ist gezählt."),
        ("Vier erfüllte Trainingswochen", 4,
         "Vier Kalenderwochen, in denen das Wochenziel vollständig erfüllt war."),
        ("Zwölf erfüllte Trainingswochen", 12,
         "Zwölf Kalenderwochen, in denen das Wochenziel vollständig erfüllt war."),
    ):
        _quest(
            db, plan, title=title, description=description,
            goal=goal, goal_title="Kraftpfad",
            quest_type="manual", period="once", target_value=target,
            repeatable=False, is_milestone=True, kind="Meilenstein", apply=apply,
        )


def _japanese(db: Session, plan: StarterPlan, apply: bool) -> None:
    goal = _goal(
        db, plan,
        slug="weg-des-japanischen",
        title="Weg des Japanischen",
        description="Tägliche SRS-Reviews und bestätigte Lernsessions.",
        short_label="Japanisch",
        apply=apply,
    )

    habit = _habit(
        db, plan,
        title="SRS-Review",
        description="Werktags eine Review-Runde im SRS. Fünf Runden ergeben die Woche.",
        goal=goal, goal_title="Weg des Japanischen",
        weekdays=JAPANESE_WEEKDAYS,
        recurrence="weekly", target_count=1,
        category="knowledge_learning", duration_size="short", effort="easy",
        apply=apply,
    )

    _quest(
        db, plan,
        title="Fünf Schriftrollen",
        description="Fünf abgeschlossene SRS-Reviews in einer Kalenderwoche.",
        goal=goal, goal_title="Weg des Japanischen",
        quest_type="habit_count", period="weekly", target_value=5,
        repeatable=True, is_milestone=False, habit=habit, apply=apply,
    )

    _quest(
        db, plan,
        title="Zwei Gespräche mit dem Sensei",
        description="Zwei bestätigte Japanisch-Sessions in einer Kalenderwoche. "
                    "Die Session selbst wird weiterhin ausschließlich über den "
                    "SAVE-Import belohnt; diese Quest gibt nur ihren eigenen Bonus.",
        goal=goal, goal_title="Weg des Japanischen",
        quest_type="japanese_session_count", period="weekly", target_value=2,
        repeatable=True, is_milestone=False, apply=apply,
    )

    for title, target, description in (
        ("Erstes SRS-Review", 1, "Die erste Review-Runde ist eingetragen."),
        ("20 SRS-Reviews", 20, "Zwanzig eingetragene Review-Runden."),
        ("Acht bestätigte Sessions", 8, "Acht über den SAVE-Import bestätigte Sessions."),
        ("Vier Wochen mit beiden Zielen", 4,
         "Vier Kalenderwochen, in denen Review- und Session-Ziel gemeinsam erfüllt waren."),
    ):
        _quest(
            db, plan, title=title, description=description,
            goal=goal, goal_title="Weg des Japanischen",
            quest_type="manual", period="once", target_value=target,
            repeatable=False, is_milestone=True, kind="Meilenstein", apply=apply,
        )


def _body_control(db: Session, plan: StarterPlan, apply: bool) -> None:
    goal = _goal(
        db, plan,
        slug="koerperkontrolle",
        title="Körperkontrolle",
        description="Neutrale Wochenroutine aus Kraft-, Atem- und Entspannungseinheiten. "
                    + SAFETY_NOTE,
        short_label="Routine K",
        apply=apply,
    )

    # The five routines already exist as the CONTROL program. They are reused by
    # exact title and only get their goal link and weekday plan added — never a
    # second copy, and never a changed title or description.
    from app.seed_programs import CONTROL

    control_habits = {h.title: h for h in CONTROL.habits}
    for title, weekday in CONTROL_SCHEDULE.items():
        source = control_habits.get(title)
        _habit(
            db, plan,
            title=title,
            description=source.description if source else "",
            goal=goal, goal_title="Körperkontrolle",
            weekdays=[weekday],
            recurrence=source.recurrence if source else "weekly",
            target_count=1,
            category=source.category if source else "technique_skill",
            duration_size=source.duration_size if source else "normal",
            effort=source.effort if source else "normal",
            apply=apply,
        )

    _quest(
        db, plan,
        title="Der Fünfer-Rhythmus",
        description="Alle fünf geplanten Einheiten einer Kalenderwoche erledigt. "
                    "Wird bestätigt, sobald die Woche vollständig ist — die "
                    "Wochenansicht zeigt, was noch offen ist.",
        goal=goal, goal_title="Körperkontrolle",
        quest_type="manual", period="weekly", target_value=5,
        repeatable=True, is_milestone=False, apply=apply,
    )

    # The four stages already exist as CONTROL quests; they are reused by title.
    for stage in CONTROL.quests:
        _quest(
            db, plan, title=stage.title, description=stage.description,
            goal=goal, goal_title="Körperkontrolle",
            quest_type="manual", period="once", target_value=stage.target_value,
            repeatable=False, is_milestone=True, kind="Meilenstein", apply=apply,
        )


# ---------------------------------------------------------------------------
# Public API — preview and activation walk the same code
# ---------------------------------------------------------------------------

def _walk(db: Session, apply: bool) -> StarterPlan:
    plan = StarterPlan(applied=apply)
    _strength(db, plan, apply)
    _japanese(db, plan, apply)
    _body_control(db, plan, apply)
    return plan


def plan_starter(db: Session) -> StarterPlan:
    """What an activation would do. Writes nothing."""
    return _walk(db, apply=False)


def _undo(db: Session, created: _Created) -> None:
    """Remove exactly the rows this run inserted, newest kind first."""
    from app.models import HabitScheduleDay

    db.rollback()
    if created.quests:
        db.query(Quest).filter(Quest.id.in_(created.quests)).delete(
            synchronize_session=False
        )
    if created.habits:
        db.query(HabitScheduleDay).filter(
            HabitScheduleDay.habit_id.in_(created.habits)
        ).delete(synchronize_session=False)
        db.query(Habit).filter(Habit.id.in_(created.habits)).delete(
            synchronize_session=False
        )
    if created.goals:
        db.query(Goal).filter(Goal.id.in_(created.goals)).delete(
            synchronize_session=False
        )
    db.commit()


def apply_starter(db: Session) -> StarterPlan:
    """Activate the campaign, all or nothing.

    On any error the rows this run created are removed again, so there is no
    half-created goal, no dangling quest and no partial weekday plan. The error
    that reaches the caller is a plain sentence — the traceback goes to the log,
    never to the browser.
    """
    plan = StarterPlan(applied=True)
    try:
        _strength(db, plan, True)
        _japanese(db, plan, True)
        _body_control(db, plan, True)
        db.commit()
        return plan
    except Exception as exc:  # noqa: BLE001 — re-raised as a sanitized error
        logger.exception("Starter campaign failed; rolling back what it created")
        try:
            _undo(db, plan.created)
        except Exception:  # noqa: BLE001
            logger.exception("Cleanup after a failed starter campaign also failed")
        raise StarterError(
            "Die Starter-Kampagne konnte nicht angelegt werden. "
            "Es wurde nichts gespeichert."
        ) from exc
