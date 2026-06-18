import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.achievements import check_achievements, seed_achievements
from app.config import get_settings
from app.database import get_db, init_db
from app.models import Achievement, HeroProfile, Quest, SyncEvent, XpEvent
from app.quests import evaluate_quests, seed_quests
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


@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    ctx = _hero_context(hero)

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
        "dashboard.html",
        {
            "request": request,
            **ctx,
            "recent_xp": recent_xp,
            "active_quests": active_quests,
            "recent_syncs": recent_syncs,
        },
    )


@app.post("/sync")
async def trigger_sync(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()

    try:
        token = settings.get_token()
    except RuntimeError as e:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                **_hero_context(_ensure_hero(db, settings.HERO_NAME)),
                "sync_error": str(e),
                "recent_xp": [],
                "active_quests": db.query(Quest).filter(Quest.active == True).all(),
                "recent_syncs": [],
            },
            status_code=400,
        )

    client = WgerClient(base_url=settings.WGER_BASE_URL, token=token)
    result = await sync_workouts(db, client, hero_name=settings.HERO_NAME)

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
    return templates.TemplateResponse(
        "quests.html",
        {"request": request, **_hero_context(hero), "quests": all_quests},
    )


@app.get("/achievements", response_class=HTMLResponse)
async def achievements_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    hero = _ensure_hero(db, settings.HERO_NAME)
    all_achievements = db.query(Achievement).order_by(Achievement.unlocked_at.desc()).all()
    return templates.TemplateResponse(
        "achievements.html",
        {"request": request, **_hero_context(hero), "achievements": all_achievements},
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
        "settings.html",
        {
            "request": request,
            "config_items": config_items,
            "last_sync": last_sync,
            "last_sync_error": last_sync_error.last_error if last_sync_error else None,
        },
    )
