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
