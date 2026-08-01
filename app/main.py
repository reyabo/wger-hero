import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.achievements import check_achievements, seed_achievements
from app.auth import (
    CSRF_FIELD,
    SESSION_COOKIE,
    AuthConfigError,
    LoginRateLimiter,
    SessionData,
    csrf_ok,
    deserialize_session,
    is_public_path,
    is_state_changing,
    load_password_hash,
    load_session_secret,
    new_session,
    serialize_session,
    verify_password,
)
from app.config import get_settings
from app.database import get_db, init_db
from app.goals import (
    GOAL_STATUSES,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_LABELS,
    STATUS_PAUSED,
    create_goal,
    get_by_slug,
    goal_progress,
    habits_for_goal,
    list_goals,
    milestones_for_goal,
    quests_for_goal,
    set_status,
    update_goal,
)
from app.habits import RECURRENCE_CHOICES, archive_habit, complete_habit, create_habit, delete_or_archive_habit, update_habit
from app.japanese_import import (
    get_latest_import,
    get_recent_imports,
    import_save,
    preview_save,
)
from app.japanese_saves import SaveParseError
from app.models import (
    Achievement,
    Goal,
    Habit,
    HabitCompletion,
    HeroProfile,
    JapaneseSaveImport,
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
    migrate_seeded_quests,
    parse_period_range,
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
from app.seed_defaults import find_legacy_japanese_habit, seed_default_habits
from app.stats import (
    STAT_KEYS,
    STATS,
    STAT_ABBR,
    build_radar,
    get_all_stat_progress,
    get_recent_stat_gains,
    get_stat_summary,
    get_stat_totals,
    parse_stat_rewards,
)
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
        migrate_seeded_quests(db)
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

_login_limiter = LoginRateLimiter()


def _csrf_input(request: Request) -> Markup:
    """Hidden CSRF field for a form. Empty string when auth (and thus CSRF) is off."""
    token = getattr(request.state, "csrf_token", "")
    if not token:
        return Markup("")
    return Markup(
        f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(token)}">'
    )


templates.env.globals["csrf_input"] = _csrf_input


def _cookie_kwargs(settings) -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": bool(settings.COOKIE_SECURE),
        "max_age": int(settings.SESSION_MAX_AGE_SECONDS),
        "path": "/",
    }


def _set_session(response, session: SessionData, settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        serialize_session(session, load_session_secret(settings)),
        **_cookie_kwargs(settings),
    )


async def _csrf_token_from_body(request: Request) -> str | None:
    """Read the CSRF field without consuming the body for the route handler.

    Middleware and route see different Request objects, so simply awaiting
    request.form() here would drain the receive stream and leave the handler
    with an empty body. Reading the raw body once and replaying it through a
    replacement receive channel keeps the request intact downstream.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        return None

    body = await request.body()

    async def replay() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = replay  # noqa: SLF001 — the documented Starlette workaround

    form = await request.form()
    token = form.get(CSRF_FIELD)
    # Re-arm the replay: request.form() consumed it again.
    request._receive = replay  # noqa: SLF001
    return token


@app.middleware("http")
async def access_control(request: Request, call_next):
    """Single gate for authentication and CSRF.

    Doing this in middleware rather than per-route means a newly added POST
    route is protected by default instead of by remembering to decorate it.
    """
    settings = get_settings()

    if not settings.AUTH_ENABLED:
        request.state.csrf_token = ""
        request.state.authenticated = True
        return await call_next(request)

    try:
        secret = load_session_secret(settings)
    except AuthConfigError as exc:
        # Fail closed: without a signing key no session can be trusted.
        logger.error("Auth is enabled but unusable: %s", exc)
        return JSONResponse({"detail": "Auth not configured"}, status_code=503)

    session = deserialize_session(
        request.cookies.get(SESSION_COOKIE), secret, settings.SESSION_MAX_AGE_SECONDS
    )
    public = is_public_path(request.url.path)

    # Everyone gets a CSRF token, including the anonymous login form.
    issue_cookie = session is None
    if session is None:
        session = new_session(authenticated=False)
    request.state.csrf_token = session.csrf
    request.state.authenticated = session.authenticated

    if is_state_changing(request.method):
        submitted = await _csrf_token_from_body(request)
        if not csrf_ok(session, submitted):
            logger.warning("Rejected %s %s: CSRF check failed", request.method, request.url.path)
            return _csrf_failure(request)

    if not public and not session.authenticated:
        if is_state_changing(request.method):
            return JSONResponse({"detail": "Not authenticated"}, status_code=403)
        return RedirectResponse(url="/login", status_code=303)

    response = await call_next(request)

    if issue_cookie:
        _set_session(response, session, settings)
    if not public:
        # Authenticated pages must never be stored by a proxy or the SW.
        response.headers["Cache-Control"] = "no-store, private"
    return response


def _csrf_failure(request: Request):
    return JSONResponse(
        {"detail": "Die Sitzung ist abgelaufen. Bitte die Seite neu laden."},
        status_code=403,
    )


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=303)
    if getattr(request.state, "authenticated", False):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
async def login_submit(request: Request):
    settings = get_settings()
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=303)

    client = request.client.host if request.client else "unknown"
    if _login_limiter.is_blocked(client):
        wait = _login_limiter.retry_after(client)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": f"Zu viele Versuche. Bitte in {wait} Sekunden erneut versuchen."
            },
            status_code=429,
        )

    form = await request.form()
    password = form.get("password") or ""

    try:
        stored = load_password_hash(settings)
    except AuthConfigError as exc:
        logger.error("Login impossible: %s", exc)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Anmeldung ist nicht konfiguriert."},
            status_code=503,
        )

    if not verify_password(password, stored):
        # Never log the attempt value, never distinguish causes to the user.
        _login_limiter.record_failure(client)
        logger.info("Failed login attempt")
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Anmeldung fehlgeschlagen."},
            status_code=401,
        )

    _login_limiter.reset(client)
    response = RedirectResponse(url="/", status_code=303)
    _set_session(response, new_session(authenticated=True), settings)
    return response


@app.post("/logout")
async def logout(request: Request):
    """POST only — a GET logout would be triggerable from any page."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


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
            "japanese_latest": get_latest_import(db),
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


def _quest_range_from_form(form) -> tuple[datetime | None, datetime | None, str | None]:
    """Resolve the explicit period window a quest form submitted.

    Only a period of "once" carries an explicit range; for every other period the
    window is derived, so we clear the stored bounds to keep a stale range from
    overriding it in _period_window().
    """
    if (form.get("period") or "weekly") != "once":
        return None, None, None
    return parse_period_range(form.get("period_start"), form.get("period_end"))


def _quest_form_error(request: Request, db: Session, form, error: str, *,
                      quest: Quest | None, action: str, heading: str):
    """Re-render the quest form with the submitted values and an error."""
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="quest_form.html",
        context={
            **_hero_context(hero),
            **_quest_form_context(quest),
            "form_action": action,
            "heading": heading,
            "error": error,
            "submitted": dict(form),
        },
        status_code=400,
    )


@app.post("/quests/new")
async def quest_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url="/quests/new", status_code=303)

    period_start, period_end, error = _quest_range_from_form(form)
    if error:
        return _quest_form_error(
            request, db, form, error,
            quest=None, action="/quests/new", heading="New Quest",
        )

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
        period_start=period_start,
        period_end=period_end,
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

    period_start, period_end, error = _quest_range_from_form(form)
    if error:
        return _quest_form_error(
            request, db, form, error,
            quest=quest, action=f"/quests/{quest.id}/edit", heading="Edit Quest",
        )

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
        period_start=period_start,
        period_end=period_end,
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


@app.get("/japanese", response_class=HTMLResponse)
async def japanese_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="japanese.html",
        context={
            **_hero_context(hero),
            "recent_imports": get_recent_imports(db),
            "latest": get_latest_import(db),
            "legacy_habit": find_legacy_japanese_habit(db),
        },
    )


@app.post("/japanese/archive-habit")
async def japanese_archive_habit(request: Request, db: Session = Depends(get_db)):
    """Archive the seeded "Japanisch lernen" habit so it stops paying out.

    Uses the existing habit service, which keeps every completion and XP event
    and only deactivates the habit when it has history.
    """
    habit = find_legacy_japanese_habit(db)
    if habit is not None:
        archive_habit(db, habit)
    return RedirectResponse(url="/japanese", status_code=303)


@app.post("/japanese/preview", response_class=HTMLResponse)
async def japanese_preview(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    form = await request.form()
    raw = (form.get("raw_save") or "").strip()
    accept_credit = bool(form.get("accept_baseline_credit"))

    try:
        preview = preview_save(db, raw, accept_baseline_credit=accept_credit)
    except SaveParseError as exc:
        return templates.TemplateResponse(
            request=request,
            name="japanese.html",
            context={
                **_hero_context(hero),
                "recent_imports": get_recent_imports(db),
                "latest": get_latest_import(db),
                "errors": exc.errors,
                "raw_save": raw,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request=request,
        name="japanese_preview.html",
        context={
            **_hero_context(hero),
            "preview": preview,
            "save": preview.save,
            "previous": preview.previous,
            "raw_save": raw,
            "accept_baseline_credit": accept_credit,
            "stat_names": STATS,
        },
    )


@app.post("/japanese/import")
async def japanese_import(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    form = await request.form()
    # The raw text is re-parsed and the delta re-derived server-side; no value
    # computed in the browser is trusted.
    raw = (form.get("raw_save") or "").strip()
    accept_credit = bool(form.get("accept_baseline_credit"))

    try:
        result = import_save(
            db,
            raw,
            accept_baseline_credit=accept_credit,
            hero_name=settings.HERO_NAME,
        )
    except SaveParseError as exc:
        return templates.TemplateResponse(
            request=request,
            name="japanese.html",
            context={
                **_hero_context(hero),
                "recent_imports": get_recent_imports(db),
                "latest": get_latest_import(db),
                "errors": exc.errors,
                "raw_save": raw,
            },
            status_code=400,
        )

    if result.is_duplicate:
        target = result.duplicate_of.id if result.duplicate_of else None
        if target is not None:
            return RedirectResponse(url=f"/japanese/imports/{target}", status_code=303)
        return RedirectResponse(url="/japanese", status_code=303)

    return RedirectResponse(url=f"/japanese/imports/{result.created.id}", status_code=303)


@app.get("/japanese/imports/{import_id}", response_class=HTMLResponse)
async def japanese_import_detail(
    import_id: int, request: Request, db: Session = Depends(get_db)
):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    record = db.get(JapaneseSaveImport, import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return templates.TemplateResponse(
        request=request,
        name="japanese_detail.html",
        context={
            **_hero_context(hero),
            "record": record,
            "stat_names": STATS,
            "record_stat_rewards": parse_stat_rewards(record.stat_rewards),
        },
    )


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

@app.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    show_archived = request.query_params.get("archived") == "1"
    goals = list_goals(db, include_archived=show_archived)
    return templates.TemplateResponse(
        request=request,
        name="goals.html",
        context={
            **_hero_context(hero),
            "goals": goals,
            "progress": {g.id: goal_progress(db, g) for g in goals},
            "status_labels": STATUS_LABELS,
            "show_archived": show_archived,
        },
    )


@app.get("/goals/new", response_class=HTMLResponse)
async def goal_new(request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    return templates.TemplateResponse(
        request=request,
        name="goal_form.html",
        context={**_hero_context(hero), "goal": None,
                 "form_action": "/goals/new", "heading": "Neues Ziel"},
    )


@app.post("/goals/new")
async def goal_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        return RedirectResponse(url="/goals/new", status_code=303)
    goal = create_goal(
        db,
        title=title,
        description=form.get("description"),
        short_label=form.get("short_label"),
        sort_order=_int_field(form, "sort_order", 0),
    )
    return RedirectResponse(url=f"/goals/{goal.slug}", status_code=303)


@app.get("/goals/{slug}", response_class=HTMLResponse)
async def goal_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    goal = get_by_slug(db, slug)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return templates.TemplateResponse(
        request=request,
        name="goal_detail.html",
        context={
            **_hero_context(hero),
            "goal": goal,
            "progress": goal_progress(db, goal),
            "habits": habits_for_goal(db, goal),
            "quests": quests_for_goal(db, goal),
            "milestones": milestones_for_goal(db, goal),
            "status_labels": STATUS_LABELS,
        },
    )


@app.get("/goals/{slug}/edit", response_class=HTMLResponse)
async def goal_edit(slug: str, request: Request, db: Session = Depends(get_db)):
    hero = _ensure_hero(db, get_settings().HERO_NAME)
    goal = get_by_slug(db, slug)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return templates.TemplateResponse(
        request=request,
        name="goal_form.html",
        context={**_hero_context(hero), "goal": goal,
                 "form_action": f"/goals/{goal.slug}/edit", "heading": "Ziel bearbeiten"},
    )


@app.post("/goals/{slug}/edit")
async def goal_update(slug: str, request: Request, db: Session = Depends(get_db)):
    goal = get_by_slug(db, slug)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    form = await request.form()
    update_goal(
        db,
        goal,
        title=(form.get("title") or goal.title),
        description=form.get("description"),
        short_label=form.get("short_label"),
        sort_order=_int_field(form, "sort_order", goal.sort_order),
    )
    return RedirectResponse(url=f"/goals/{goal.slug}", status_code=303)


@app.post("/goals/{slug}/status")
async def goal_set_status(slug: str, request: Request, db: Session = Depends(get_db)):
    """Pause, resume, complete or archive. Never deletes anything."""
    goal = get_by_slug(db, slug)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    form = await request.form()
    target = (form.get("status") or "").strip()
    if not set_status(db, goal, target):
        raise HTTPException(status_code=400, detail="Unzulässiger Statuswechsel")
    return RedirectResponse(url=f"/goals/{goal.slug}", status_code=303)


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    stat_progress = get_all_stat_progress(db)
    recent_gains = get_recent_stat_gains(db, limit=20)
    summary = get_stat_summary(db)

    radar = build_radar(stat_progress)

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={
            **_hero_context(hero),
            "stat_progress": stat_progress,
            "recent_gains": recent_gains,
            "summary": summary,
            "stat_names": STATS,
            "stat_abbr": STAT_ABBR,
            "radar": radar,
        },
    )


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
