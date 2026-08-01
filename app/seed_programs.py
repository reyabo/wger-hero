"""
Optional, opt-in training programs.

Unlike ``seed_defaults``, nothing here runs on startup. A program is seeded only
when the user asks for it explicitly:

    python -m app.seed_programs <key>

A program is just a weekly habit rhythm plus a few finite quests for its stage
transitions — it introduces no new model and no new mechanic. Habits carry the
recurring rhythm ("what is due on which day"), quests carry the milestones that
end once reached.

Seeding is idempotent: a habit or quest whose exact title already exists is
skipped, so re-running never duplicates anything and never touches rows the user
has since edited or archived.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProgramHabit:
    title: str
    description: str
    category: str
    duration_size: str
    effort: str
    recurrence: str = "weekly"


@dataclass(frozen=True)
class ProgramQuest:
    title: str
    description: str
    target_value: int = 1


@dataclass(frozen=True)
class Program:
    key: str
    label: str
    habits: list[ProgramHabit] = field(default_factory=list)
    quests: list[ProgramQuest] = field(default_factory=list)


# ---------------------------------------------------------------------------
# "control" — a weekly control / breath / relaxation rhythm in four stages
# ---------------------------------------------------------------------------

CONTROL = Program(
    key="control",
    label="Kontrollprogramm",
    habits=[
        ProgramHabit(
            title="Kontrolltraining – Kraft",
            description="Mo · Kraftprotokoll und Lösen im Wechsel. "
                        "Auf jede Minute Anspannung eine Minute Entspannung.",
            category="technique_skill",
            duration_size="short",
            effort="normal",
        ),
        ProgramHabit(
            title="Atem & Wahrnehmung",
            description="Di · Zwerchfellatmung 4/8 und Skalen-Eichung mit Protokoll. "
                        "Marker benennen, nicht das Gefühl.",
            category="recovery",
            duration_size="normal",
            effort="normal",
        ),
        ProgramHabit(
            title="Kontrollsession",
            description="Mi · Treppenprotokoll: aufbauen, halten, bewusst zurückregeln. "
                        "Runterregeln immer in der festen Reihenfolge.",
            category="technique_skill",
            duration_size="long",
            effort="demanding",
        ),
        ProgramHabit(
            title="Lösen & Entspannen",
            description="Do · Nur Entspannung, keine Belastung. "
                        "Umkehrarbeit gegen Überspannung.",
            category="recovery",
            duration_size="short",
            effort="easy",
        ),
        ProgramHabit(
            title="Lange Kontrollsession",
            description="Sa · Hauptsession mit Protokoll. "
                        "Spalte „Marker/Notiz“ ist die wichtigste.",
            category="technique_skill",
            duration_size="large",
            effort="demanding",
        ),
    ],
    quests=[
        ProgramQuest(
            title="Stufe 1 – Differenzierung",
            description="Die drei Muskelgruppen lassen sich einzeln ansteuern, "
                        "und vollständiges Lösen gelingt. 2× täglich 5 Minuten, "
                        "bis der Isolationstest sauber ist.",
        ),
        ProgramQuest(
            title="Stufe 2 – Skala geeicht",
            description="Zwei Wochen Protokoll geführt; die körperlichen Marker "
                        "je Stufe sind bekannt und werden zuverlässig erkannt. "
                        "Zwerchfellatmung auch unter Belastung haltbar.",
        ),
        ProgramQuest(
            title="Stufe 3 – Präzision",
            description="Dreimal in Folge die obere Arbeitszone erreicht und wieder "
                        "kontrolliert zurückgeregelt — in drei aufeinanderfolgenden "
                        "Sessions. Zählt eine Session je Nachweis.",
            target_value=3,
        ),
        ProgramQuest(
            title="Stufe 4 – Erste Serie",
            description="Zwei saubere Durchgänge in einer Session, mit Pause dazwischen. "
                        "Realistisch erst nach mehreren Monaten — Rückschläge sind "
                        "Datenerhebung, kein Scheitern.",
            target_value=2,
        ),
    ],
)

PROGRAMS: dict[str, Program] = {CONTROL.key: CONTROL}


def seed_program(db, key: str) -> tuple[int, int]:
    """Seed one program. Returns (habits_created, quests_created).

    Idempotent: entries whose exact title already exists are skipped.
    Raises KeyError for an unknown program key.
    """
    from app.habits import create_habit
    from app.models import Habit, Quest
    from app.quests import create_quest

    program = PROGRAMS[key]

    existing_habits = {h.title for h in db.query(Habit).all()}
    existing_quests = {q.title for q in db.query(Quest).all()}

    habits_created = 0
    for item in program.habits:
        if item.title in existing_habits:
            continue
        create_habit(
            db,
            title=item.title,
            description=item.description,
            recurrence=item.recurrence,
            category=item.category,
            duration_size=item.duration_size,
            effort=item.effort,
        )
        habits_created += 1

    quests_created = 0
    for item in program.quests:
        if item.title in existing_quests:
            continue
        create_quest(
            db,
            title=item.title,
            description=item.description,
            quest_type="manual",
            period="once",
            target_value=item.target_value,
            category="technique_skill",
            duration_size="normal",
            effort="demanding",
        )
        quests_created += 1

    return habits_created, quests_created


if __name__ == "__main__":
    import os
    import sys

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    from app.database import get_db, init_db

    args = sys.argv[1:]
    if not args or args[0] not in PROGRAMS:
        print(f"Usage: python -m app.seed_programs {{{'|'.join(PROGRAMS)}}}")
        sys.exit(1)

    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        h, q = seed_program(db, args[0])
        print(f"{PROGRAMS[args[0]].label}: {h} Habit(s), {q} Quest(s) angelegt.")
        if not h and not q:
            print("Nichts angelegt — alle Einträge waren bereits vorhanden.")
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
