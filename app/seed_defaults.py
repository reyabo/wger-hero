"""Seed default habits and system data."""

DEFAULT_HABITS = [
    {"title": "30 Minuten lesen", "category": "knowledge_learning", "duration_size": "normal", "effort": "normal", "recurrence": "flexible"},
    {"title": "Japanisch lernen", "category": "knowledge_learning", "duration_size": "normal", "effort": "demanding", "recurrence": "flexible"},
    {"title": "Mobility 10 Minuten", "category": "mobility", "duration_size": "short", "effort": "normal", "recurrence": "flexible"},
    {"title": "Workout erledigen", "category": "strength_training", "duration_size": "long", "effort": "demanding", "recurrence": "flexible"},
    {"title": "30 Minuten laufen", "category": "endurance", "duration_size": "normal", "effort": "demanding", "recurrence": "flexible"},
    {"title": "Theatertraining", "category": "creativity", "duration_size": "long", "effort": "normal", "recurrence": "flexible"},
    {"title": "Projektarbeit 45 Minuten", "category": "project_work", "duration_size": "long", "effort": "normal", "recurrence": "flexible"},
    {"title": "Haushalt 20 Minuten", "category": "household_order", "duration_size": "short", "effort": "normal", "recurrence": "flexible"},
    {"title": "Erholungsspaziergang", "category": "recovery", "duration_size": "normal", "effort": "easy", "recurrence": "flexible"},
    {"title": "Schlafroutine eingehalten", "category": "recovery", "duration_size": "short", "effort": "easy", "recurrence": "flexible"},
]


def seed_default_habits(db) -> int:
    """Seed default habits only if habit table is empty. Returns count seeded."""
    from app.models import Habit
    from app.habits import create_habit
    if db.query(Habit).count() > 0:
        return 0
    for h in DEFAULT_HABITS:
        create_habit(db, **h)
    return len(DEFAULT_HABITS)


if __name__ == "__main__":
    import os
    import sys

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    from app.database import get_db, init_db

    args = sys.argv[1:]
    if not args or args[0] != "habits":
        print("Usage: python -m app.seed_defaults habits")
        sys.exit(1)

    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        n = seed_default_habits(db)
        if n:
            print(f"Seeded {n} default habits.")
        else:
            print("Habit table already populated — nothing seeded.")
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
