import logging
from datetime import date
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_SECRET_FILE = Path("/run/secrets/wger_api_token")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    WGER_BASE_URL: str = "https://wger.example.com"
    WGER_API_TOKEN: Optional[str] = None
    WGER_API_TOKEN_FILE: Optional[str] = None
    DATABASE_URL: str = "sqlite:////data/wger_hero.db"
    HERO_NAME: str = "Hero"
    APP_ENV: str = "production"
    # Set to false to skip /api/v2/log/ and exercise catalog fetching entirely
    WGER_FETCH_EXERCISE_LOGS: bool = True
    # Only sync sessions on or after this date (ISO format: YYYY-MM-DD). Empty = all history.
    SYNC_FROM_DATE: Optional[date] = None

    # --- Single-user access protection -------------------------------------
    # Disabling this also disables CSRF enforcement: without a session there is
    # nothing to bind a token to, and an unauthenticated app has no state worth
    # protecting from cross-site requests. Intended for local tests only.
    AUTH_ENABLED: bool = True
    # Argon2 hash of the one password, from a read-only mounted secret file.
    # Never a plaintext password, never the hash itself in env.
    AUTH_PASSWORD_HASH_FILE: Optional[str] = None
    # Random key signing the session cookie, from a secret file.
    SESSION_SECRET_FILE: Optional[str] = None
    SESSION_MAX_AGE_SECONDS: int = 604800  # 7 days
    COOKIE_SECURE: bool = True
    APP_TIMEZONE: str = "Europe/Berlin"
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 300

    def get_token(self) -> str:
        # Prefer explicit file path, then Docker secret, then env var
        token_file: Optional[Path] = None

        if self.WGER_API_TOKEN_FILE:
            token_file = Path(self.WGER_API_TOKEN_FILE)
        elif _SECRET_FILE.exists():
            token_file = _SECRET_FILE

        if token_file is not None:
            try:
                token = token_file.read_text().strip()
                if not token:
                    raise ValueError(f"Token file {token_file} is empty")
                return token
            except OSError as e:
                raise RuntimeError(f"Cannot read token file: {e}") from e

        if self.WGER_API_TOKEN:
            return self.WGER_API_TOKEN

        raise RuntimeError(
            "No wger API token configured. Set WGER_API_TOKEN, WGER_API_TOKEN_FILE, "
            "or mount a secret at /run/secrets/wger_api_token."
        )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
