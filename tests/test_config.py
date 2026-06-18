"""Test Settings token loading logic."""

import os
import tempfile
from pathlib import Path

import pytest

from app.config import Settings


class TestTokenLoading:
    def test_token_from_env_var(self, monkeypatch):
        monkeypatch.setenv("WGER_API_TOKEN", "my-secret-token")
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        settings = Settings()
        assert settings.get_token() == "my-secret-token"

    def test_token_from_file(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-based-token\n")
        monkeypatch.setenv("WGER_API_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        monkeypatch.delenv("WGER_API_TOKEN", raising=False)
        settings = Settings()
        assert settings.get_token() == "file-based-token"

    def test_file_takes_priority_over_env(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token")
        monkeypatch.setenv("WGER_API_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("WGER_API_TOKEN", "env-token")
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        settings = Settings()
        assert settings.get_token() == "file-token"

    def test_raises_when_no_token(self, monkeypatch):
        monkeypatch.delenv("WGER_API_TOKEN", raising=False)
        monkeypatch.delenv("WGER_API_TOKEN_FILE", raising=False)
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        settings = Settings(_env_file=None)
        settings.WGER_API_TOKEN = None
        settings.WGER_API_TOKEN_FILE = None
        with pytest.raises(RuntimeError, match="No wger API token"):
            settings.get_token()

    def test_empty_token_file_raises(self, tmp_path, monkeypatch):
        token_file = tmp_path / "empty.txt"
        token_file.write_text("   \n")
        monkeypatch.setenv("WGER_API_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        monkeypatch.delenv("WGER_API_TOKEN", raising=False)
        settings = Settings()
        with pytest.raises((ValueError, RuntimeError)):
            settings.get_token()

    def test_default_database_url(self, monkeypatch):
        monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = Settings()
        assert settings.DATABASE_URL == "sqlite:////data/wger_hero.db"
