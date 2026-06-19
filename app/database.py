import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None

# Columns added to pre-existing tables after the initial release. create_all()
# never ALTERs existing tables, so we add missing columns by hand. DDL defaults
# are constants only (SQLite forbids non-constant ADD COLUMN defaults); runtime
# values come from the Python-side defaults on the models.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "quests": [
        ("period", "VARCHAR(20)"),
        ("match_text", "VARCHAR(200)"),
        ("stat_rewards", "TEXT"),
        ("repeatable", "BOOLEAN DEFAULT 0"),
        ("created_at", "DATETIME"),
        ("updated_at", "DATETIME"),
        ("category", "VARCHAR(50)"),
        ("duration_size", "VARCHAR(20)"),
        ("effort", "VARCHAR(20)"),
    ],
    "habits": [
        ("category", "VARCHAR(50)"),
        ("duration_size", "VARCHAR(20)"),
        ("effort", "VARCHAR(20)"),
    ],
}


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        db_url = settings.DATABASE_URL

        # Ensure /data directory exists for SQLite file
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        _engine = create_engine(db_url, connect_args={"check_same_thread": False})

        # Enable WAL mode for better concurrent access
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return _engine


def _get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_get_engine())
    return _SessionLocal


def _migrate_added_columns() -> None:
    """Add columns introduced after launch to tables that already exist.

    Idempotent and safe on a fresh database: create_all() builds new tables with
    every column, so this only fires for older databases missing a column.
    """
    engine = _get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all() already built it with all columns
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("Migrated: added column %s.%s", table, name)


def init_db() -> None:
    Base.metadata.create_all(bind=_get_engine())
    _migrate_added_columns()


def get_db() -> Generator[Session, None, None]:
    db = _get_session_factory()()
    try:
        yield db
    finally:
        db.close()
