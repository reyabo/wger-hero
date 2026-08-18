"""The FitTrackee CLI — including the guards that stop a costly mistake.

`sync` before `baseline` would award XP for the whole history, and a second
`baseline` would be meaningless. Both are refused with an exit code, not just a
warning, so a deploy script cannot walk past them.

Nothing here reaches the network: the two commands that would are exercised
through their guards, which fire before any client is built.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.fittrackee_sync import _main, _status_lines, get_connection, has_baseline
from app.models import Base, FitTrackeeSport, FitTrackeeWorkout, HeroProfile

MODULE = Path(__file__).resolve().parents[1] / "app" / "fittrackee_sync.py"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(HeroProfile(name="Hero", level=1, total_xp=0))
    session.commit()
    yield session
    session.close()


@pytest.fixture
def cli(db, monkeypatch, tmp_path):
    """Point the CLI at the in-memory database and an empty token store."""
    import app.config as cfg
    import app.database as database
    import app.fittrackee_sync as sync_module

    settings = cfg.get_settings()
    monkeypatch.setattr(settings, "FITTRACKEE_TOKEN_DIR", str(tmp_path / "oauth"))
    monkeypatch.setattr(settings, "FITTRACKEE_BASE_URL", "https://ft.example.com")
    monkeypatch.setattr(settings, "FITTRACKEE_CLIENT_ID", "cid")

    def fake_get_db():
        yield db

    monkeypatch.setattr(database, "get_db", fake_get_db)
    monkeypatch.setattr(database, "init_db", lambda: None)
    return db


class _Settings:
    FITTRACKEE_BASE_URL = None
    FITTRACKEE_CLIENT_ID = None
    FITTRACKEE_TOKEN_DIR = None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_reports_without_contacting_anything(cli, capsys):
    assert _main(["status"]) == 0
    assert "FitTrackee-Status" in capsys.readouterr().out


def test_status_says_there_is_no_baseline(cli, capsys):
    _main(["status"])
    assert "noch nicht erstellt" in capsys.readouterr().out


def test_status_counts_workouts(db, cli, capsys):
    db.add(
        FitTrackeeWorkout(
            external_id="a", workout_at=datetime(2026, 8, 18, 9, 0),
            local_date=datetime(2026, 8, 18).date(), sport_id=5,
            duration_seconds=1800, qualifies_for_endurance=True,
            reward_eligible=True, source_hash="h",
        )
    )
    db.commit()

    _main(["status"])
    out = capsys.readouterr().out
    assert "Workouts gespeichert:    1" in out
    assert "davon reward-fähig:      1" in out


def test_status_never_prints_a_token(db, tmp_path):
    from app.fittrackee_oauth import TokenSet, TokenStore

    class S(_Settings):
        FITTRACKEE_TOKEN_DIR = str(tmp_path / "oauth")

    TokenStore(tmp_path / "oauth").save(TokenSet("secret-access", "secret-refresh"))

    text = "\n".join(_status_lines(db, S))
    assert "secret-access" not in text
    assert "secret-refresh" not in text


def test_status_reports_only_whether_a_token_exists(db, tmp_path):
    from app.fittrackee_oauth import TokenSet, TokenStore

    class S(_Settings):
        FITTRACKEE_TOKEN_DIR = str(tmp_path / "oauth")

    assert "Autorisierung vorhanden: nein" in "\n".join(_status_lines(db, S))
    TokenStore(tmp_path / "oauth").save(TokenSet("a", "r"))
    assert "Autorisierung vorhanden: ja" in "\n".join(_status_lines(db, S))


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

def test_sync_before_baseline_is_refused(cli, capsys):
    """The expensive mistake: it would award the entire history."""
    assert _main(["sync"]) == 2
    assert "keine Baseline" in capsys.readouterr().out


def test_a_second_baseline_is_refused(db, cli, capsys):
    get_connection(db).baseline_at = datetime(2026, 8, 18, 12, 0)
    db.commit()

    assert _main(["baseline"]) == 0
    assert "bereits eine Baseline" in capsys.readouterr().out


def test_without_authorization_the_commands_stop(cli, capsys):
    assert _main(["probe"]) == 2
    assert "FEHLER" in capsys.readouterr().out


def test_the_guard_runs_before_any_network_call(cli, capsys):
    """sync is refused for the missing baseline, not for the missing token —
    proving the order: no client is built before the guard has passed."""
    assert _main(["sync"]) == 2
    out = capsys.readouterr().out
    assert "Baseline" in out


def test_has_baseline_reflects_the_connection(db):
    assert not has_baseline(db)
    get_connection(db).baseline_at = datetime(2026, 8, 18, 12, 0)
    db.commit()
    assert has_baseline(db)


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------

def test_an_unknown_command_is_a_usage_error(cli):
    with pytest.raises(SystemExit) as excinfo:
        _main(["frobnicate"])
    assert excinfo.value.code == 2


def test_the_documented_commands_all_parse(cli):
    """Guard against a rename that would silently break DEPLOY.md."""
    import argparse

    for command in ("status", "probe", "baseline", "sync"):
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=("status", "probe", "baseline", "sync"))
        parser.add_argument("--dry-run", action="store_true")
        assert parser.parse_args([command]).command == command


def test_dry_run_is_accepted(cli, capsys):
    """It still hits the baseline guard, which is what we want to see."""
    assert _main(["sync", "--dry-run"]) == 2


def test_the_module_runs_as_a_script():
    result = subprocess.run(
        [sys.executable, "-m", "app.fittrackee_sync", "--help"],
        capture_output=True, text=True,
        env={"WGER_BASE_URL": "https://wger.example.com", "PATH": "/usr/bin:/bin",
             "AUTH_ENABLED": "false"},
        cwd=MODULE.parents[1],
    )
    assert result.returncode == 0
    assert "fittrackee_sync" in result.stdout


# ---------------------------------------------------------------------------
# The module keeps its promises
# ---------------------------------------------------------------------------

def test_the_module_never_prints_a_workout_note():
    """Notes never reach this module, and nothing here would print one."""
    source = MODULE.read_text()
    for field in ("notes", "description", "gpx", "map", "bounds"):
        assert f'"{field}"' not in source


def test_the_docs_document_every_command():
    readme = (MODULE.parents[1] / "README.md").read_text()
    for command in ("status", "probe", "baseline", "sync"):
        assert f"python -m app.fittrackee_sync {command}" in readme


def test_the_deploy_doc_uses_the_baseline_dry_run_first():
    deploy = (MODULE.parents[1] / "docs" / "DEPLOY.md").read_text()
    assert "fittrackee_sync baseline --dry-run" in deploy
    assert deploy.index("baseline --dry-run") < deploy.index("fittrackee_sync sync")
