import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.achievements import check_achievements, seed_achievements
from app.config import get_settings
from app.database import get_db, init_db
from app.habits import RECURRENCE_CHOICES, complete_habit, create_habit, delete_or_archive_habit, update_habit
from app.models import (
    Achievement,
    Habit,
    HabitCompletion,
    HeroProfile,
    Quest,
    SyncEvent,
    XpEvent,
)
from app.quests import (
    PERIOD_CHOICES,
    QUEST_TYPE_CHOICES,
    complete_quest_manual,
    create_quest,
    delete_or_archive_quest,
    evaluate_quests,
    seed_quests,
    update_quest,
)
from app.rewards import (
    CATEGORIES,
    CATEGORY_CHOICES,
    DURATION_CHOICES,
    DURATION_LABELS,
    EFFORT_CHOICES,
    EFFORT_LABELS,
    calculate_rewards,
)
from app.seed_defaults import seed_default_habits
from app.stats import STAT_KEYS, STATS, get_stat_totals, parse_stat_rewards
from app.sync import sync_workouts
from app.wger_client import WgerClient
from app.xp import level_from_total_xp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        settings = get_settings()
        _ensure_hero(db, settings.HERO_NAME)
        seed_quests(db)
        seed_achievements(db)
        seed_default_habits(db)
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass
    yield


def _ensure_hero(db: Session, name: str) -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.commit()
        db.refresh(hero)
    return hero


app = FastAPI(title="wger-hero", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _hero_context(hero: HeroProfile) -> dict:
    level, xp_in_level, xp_needed = level_from_total_xp(hero.total_xp)
    pct = int((xp_in_level / xp_needed) * 100) if xp_needed else 0
    return {
        "hero": hero,
        "level": level,
        "xp_in_level": xp_in_level,
        "xp_needed": xp_needed,
        "xp_pct": pct,
    }


def _stat_rewards_from_form(form) -> dict[str, int]:
    """Extract {stat_key: xp} from `stat_<key>` form fields (positive ints only)."""
    rewards: dict[str, int] = {}
    for key in STAT_KEYS:
        raw = form.get(f"stat_{key}")
        if not raw:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            rewards[key] = value
    return rewards


def _checkbox(form, name: str) -> bool:
    return form.get(name) is not None


def _int_field(form, name: str, default: int = 0) -> int:
    try:
        return int(form.get(name))
    except (TypeError, ValueError):
        return default


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)

    recent_xp = (
        db.query(XpEvent)
        .order_by(XpEvent.created_at.desc())
        .limit(10)
        .all()
    )
    active_quests = (
        db.query(Quest)
        .filter(Quest.active == True)
        .all()
    )
    recent_syncs = (
        db.query(SyncEvent)
        .order_by(SyncEvent.synced_at.desc())
        .limit(5)
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **_hero_context(hero),
            "recent_xp": recent_xp,
            "active_quests": active_quests,
            "recent_syncs": recent_syncs,
            "stat_totals": get_stat_totals(db),
            "stat_names": STATS,
        },
    )


@app.post("/sync")
async def trigger_sync(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()

    try:
        token = settings.get_token()
    except RuntimeError as e:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                **_hero_context(_ensure_hero(db, settings.HERO_NAME)),
                "sync_error": str(e),
                "recent_xp": [],
                "active_quests": db.query(Quest).filter(Quest.active == True).all(),
                "recent_syncs": [],
            },
            status_code=400,
        )

    client = WgerClient(base_url=settings.WGER_BASE_URL, token=token)
    result = await sync_workouts(
        db,
        client,
        hero_name=settings.HERO_NAME,
        fetch_exercise_logs=settings.WGER_FETCH_EXERCISE_LOGS,
        sync_from_date=settings.SYNC_FROM_DATE,
    )

    # Store sanitized error on the most recent SyncEvent if errors occurred
    if result.errors:
        last_event = (
            db.query(SyncEvent)
            .filter(SyncEvent.source == "wger")
            .order_by(SyncEvent.synced_at.desc())
            .first()
        )
        if last_event:
            last_event.last_error = result.errors[-1]
            db.commit()

    hero = db.query(HeroProfile).first()
    if hero:
        evaluate_quests(db, hero)
        check_achievements(db, hero)
        db.commit()

    return RedirectResponse(url="/", status_code=303)


@app.get("/quests", response_class=HTMLResponse)
async def quests_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    all_quests = db.query(Quest).order_by(Quest.active.desc(), Quest.completed_at.desc()).all()
    quest_rewards = {q.id: parse_stat_rewards(q.stat_rewards) for q in all_quests}
    return templates.TemplateResponse(
        request=request,
        name="quests.html",
        context={
            **_hero_context(hero),
            "quests": all_quests,
            "quest_rewards": quest_rewards,
            "stat_names": STATS,
        },
    )


def _quest_form_context(quest: Quest | None) -> dict:
    from app.rewards import CATEGORIES, DURATION_LABELS, EFFORT_LABELS, CATEGORY_CHOICES, DURATION_CHOICES, EFFORT_CHOICES
    return {
        "quest": quest,
        "rewards": parse_stat_rewards(quest.stat_rewards) if quest else {},
        "quest_types": QUEST_TYPE_CHOICES,
        "periods": PERIOD_CHOICES,
        "stat_keys": STAT_KEYS,
        "stat_names": STATS,
        "categories": CATEGORIES,
        "duration_labels": DURATION_LABELS,
        "effort_labels": EFFORT_LABELS,
        "category_choices": CATEGORY_CHOICES,
        "duration_choices": DURATION_CHOICES,
        "effort_choices": EFFORT_CHOICES,
    }


@app.get("/quests/new", response_class=HTMLResponse)
async def quest_new(request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="quest_form.html",
        context={
            **_hero_context(hero),
            **_quest_form_context(None),
            "form_action": "/quests/new",
            "heading": "New Quest",
        },
    )


@app.post("/quests/new")
async def quest_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url="/quests/new", status_code=303)
    create_quest(
        db,
        title=title,
        description=form.get("description"),
        quest_type=form.get("quest_type") or "manual",
        period=form.get("period") or "weekly",
        target_value=_int_field(form, "target_value", 1),
        match_text=form.get("match_text"),
        xp_reward=_int_field(form, "xp_reward", 0),
        stat_rewards=_stat_rewards_from_form(form),
        repeatable=_checkbox(form, "repeatable"),
        active=_checkbox(form, "active"),
    )
    return RedirectResponse(url="/quests", status_code=303)


@app.get("/quests/{quest_id}/edit", response_class=HTMLResponse)
async def quest_edit(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.get(Quest, quest_id)
    if quest is None:
        raise HTTPException(status_code=404, detail="Quest not found")
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="quest_form.html",
        context={
            **_hero_context(hero),
            **_quest_form_context(quest),
            "form_action": f"/quests/{quest_id}/edit",
            "heading": "Edit Quest",
        },
    )


@app.post("/quests/{quest_id}/edit")
async def quest_update(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.get(Quest, quest_id)
    if quest is None:
        raise HTTPException(status_code=404, detail="Quest not found")
    form = await request.form()
    update_quest(
        db,
        quest,
        title=(form.get("title") or quest.title),
        description=form.get("description"),
        quest_type=form.get("quest_type") or quest.quest_type,
        period=form.get("period") or quest.period,
        target_value=_int_field(form, "target_value", quest.target_value),
        match_text=form.get("match_text"),
        xp_reward=_int_field(form, "xp_reward", quest.xp_reward),
        stat_rewards=_stat_rewards_from_form(form),
        repeatable=_checkbox(form, "repeatable"),
        active=_checkbox(form, "active"),
    )
    return RedirectResponse(url="/quests", status_code=303)


@app.post("/quests/{quest_id}/complete")
async def quest_complete(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.get(Quest, quest_id)
    if quest is None:
        raise HTTPException(status_code=404, detail="Quest not found")
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    complete_quest_manual(db, quest, hero)
    check_achievements(db, hero)
    return RedirectResponse(url="/quests", status_code=303)


@app.get("/habits", response_class=HTMLResponse)
async def habits_page(request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    habits = db.query(Habit).order_by(Habit.active.desc(), Habit.title).all()
    habit_rewards = {h.id: parse_stat_rewards(h.stat_rewards) for h in habits}
    completion_counts = {
        h.id: db.query(HabitCompletion).filter(HabitCompletion.habit_id == h.id).count()
        for h in habits
    }
    return templates.TemplateResponse(
        request=request,
        name="habits.html",
        context={
            **_hero_context(hero),
            "habits": habits,
            "habit_rewards": habit_rewards,
            "completion_counts": completion_counts,
            "stat_names": STATS,
        },
    )


def _habit_form_context(habit: Habit | None) -> dict:
    from app.rewards import CATEGORIES, DURATION_LABELS, EFFORT_LABELS, CATEGORY_CHOICES, DURATION_CHOICES, EFFORT_CHOICES
    return {
        "habit": habit,
        "rewards": parse_stat_rewards(habit.stat_rewards) if habit else {},
        "recurrences": RECURRENCE_CHOICES,
        "stat_keys": STAT_KEYS,
        "stat_names": STATS,
        "categories": CATEGORIES,
        "duration_labels": DURATION_LABELS,
        "effort_labels": EFFORT_LABELS,
        "category_choices": CATEGORY_CHOICES,
        "duration_choices": DURATION_CHOICES,
        "effort_choices": EFFORT_CHOICES,
    }


@app.get("/habits/new", response_class=HTMLResponse)
async def habit_new(request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="habit_form.html",
        context={
            **_hero_context(hero),
            **_habit_form_context(None),
            "form_action": "/habits/new",
            "heading": "New Habit",
        },
    )


@app.post("/habits/new")
async def habit_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url="/habits/new", status_code=303)
    raw_xp = form.get("base_xp_reward")
    base_xp = _int_field(form, "base_xp_reward", 0) if raw_xp else None
    create_habit(
        db,
        title=title,
        description=form.get("description"),
        active=_checkbox(form, "active"),
        recurrence=form.get("recurrence") or "daily",
        target_count=_int_field(form, "target_count", 1),
        base_xp_reward=base_xp,
        stat_rewards=_stat_rewards_from_form(form),
        category=form.get("category") or None,
        duration_size=form.get("duration_size") or None,
        effort=form.get("effort") or None,
    )
    return RedirectResponse(url="/habits", status_code=303)


@app.get("/habits/{habit_id}/edit", response_class=HTMLResponse)
async def habit_edit(habit_id: int, request: Request, db: Session = Depends(get_db)):
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="Habit not found")
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="habit_form.html",
        context={
            **_hero_context(hero),
            **_habit_form_context(habit),
            "form_action": f"/habits/{habit_id}/edit",
            "heading": "Edit Habit",
        },
    )


@app.post("/habits/{habit_id}/edit")
async def habit_update(habit_id: int, request: Request, db: Session = Depends(get_db)):
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="Habit not found")
    form = await request.form()
    raw_xp = form.get("base_xp_reward")
    base_xp = _int_field(form, "base_xp_reward", habit.base_xp_reward) if raw_xp else None
    update_habit(
        db,
        habit,
        title=(form.get("title") or habit.title),
        description=form.get("description"),
        active=_checkbox(form, "active"),
        recurrence=form.get("recurrence") or habit.recurrence,
        target_count=_int_field(form, "target_count", habit.target_count),
        base_xp_reward=base_xp,
        stat_rewards=_stat_rewards_from_form(form),
        category=form.get("category") or None,
        duration_size=form.get("duration_size") or None,
        effort=form.get("effort") or None,
    )
    return RedirectResponse(url="/habits", status_code=303)


@app.post("/habits/{habit_id}/complete")
async def habit_complete(habit_id: int, request: Request, db: Session = Depends(get_db)):
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="Habit not found")
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    complete_habit(db, habit, hero)
    # Habit completions may advance habit_count quests and unlock achievements.
    evaluate_quests(db, hero)
    check_achievements(db, hero)
    return RedirectResponse(url="/habits", status_code=303)



@app.post("/habits/{habit_id}/delete")
async def habit_delete(habit_id: int, request: Request, db: Session = Depends(get_db)):
    habit = db.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="Habit not found")
    delete_or_archive_habit(db, habit)
    return RedirectResponse(url="/habits", status_code=303)


@app.post("/quests/{quest_id}/delete")
async def quest_delete(quest_id: int, request: Request, db: Session = Depends(get_db)):
    quest = db.get(Quest, quest_id)
    if quest is None:
        raise HTTPException(status_code=404, detail="Quest not found")
    delete_or_archive_quest(db, quest)
    return RedirectResponse(url="/quests", status_code=303)


@app.get("/achievements", response_class=HTMLResponse)
async def achievements_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    all_achievements = db.query(Achievement).order_by(Achievement.unlocked_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="achievements.html",
        context={**_hero_context(hero), "achievements": all_achievements},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        token_ok = bool(settings.get_token())
        token_status = "Configured"
    except RuntimeError:
        token_ok = False
        token_status = "Not configured"

    config_items = [
        ("WGER_BASE_URL", settings.WGER_BASE_URL, True),
        ("API Token", token_status, token_ok),
        ("DATABASE_URL", settings.DATABASE_URL, True),
        ("HERO_NAME", settings.HERO_NAME, True),
        ("APP_ENV", settings.APP_ENV, True),
        ("WGER_FETCH_EXERCISE_LOGS", str(settings.WGER_FETCH_EXERCISE_LOGS), True),
        ("SYNC_FROM_DATE", settings.SYNC_FROM_DATE.isoformat() if settings.SYNC_FROM_DATE else "all history", True),
    ]

    # Last sync status — show only sanitized summary, never raw payloads
    last_sync = (
        db.query(SyncEvent)
        .filter(SyncEvent.source == "wger")
        .order_by(SyncEvent.synced_at.desc())
        .first()
    )
    last_sync_error = (
        db.query(SyncEvent)
        .filter(SyncEvent.source == "wger", SyncEvent.last_error.isnot(None))
        .order_by(SyncEvent.synced_at.desc())
        .first()
    )

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "config_items": config_items,
            "last_sync": last_sync,
            "last_sync_error": last_sync_error.last_error if last_sync_error else None,
        },
    )
