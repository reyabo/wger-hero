import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Point the app at an in-memory database before anything imports app.config.
# The production default is sqlite:////data/wger_hero.db, and _get_engine()
# creates the parent directory on first use — which fails with PermissionError
# on CI, where the runner may not write to /data. Route tests override get_db(),
# but the FastAPI lifespan still calls init_db(), so the engine is built either
# way. "sqlite://" is pure in-memory and skips the mkdir path entirely.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")

from app.models import Base  # noqa: E402  (import after env defaults)


@pytest.fixture(scope="function")
def db():
    """In-memory SQLite session for tests."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
