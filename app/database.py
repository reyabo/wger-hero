from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine = None
_SessionLocal = None


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


def init_db() -> None:
    Base.metadata.create_all(bind=_get_engine())


def get_db() -> Generator[Session, None, None]:
    db = _get_session_factory()()
    try:
        yield db
    finally:
        db.close()
