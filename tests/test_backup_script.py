"""scripts/backup.sh — the one script that writes.

No docker and no network here. What matters is testable without either: the
rule deciding which files rotation may delete, and the guards that keep a
failed backup from triggering one.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "backup.sh"
TEXT = SCRIPT.read_text()

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

# The rotation predicate, copied from the script so the test fails loudly if
# the script's own pattern ever drifts from what is asserted here.
ROTATION_GLOB = (
    "auto-backup-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    "_[0-9][0-9]-[0-9][0-9]-[0-9][0-9].sqlite"
)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@needs_bash
def test_the_script_is_syntactically_valid():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_the_script_aborts_on_the_first_error():
    """A smoke test surveys everything; a backup must stop. Different rule."""
    assert "set -euo pipefail" in TEXT
    assert "set -uo pipefail\n" not in TEXT


def test_the_script_does_not_swallow_its_output():
    """Under cron, stderr and the exit code are the alarm."""
    assert "exec >" not in TEXT


def test_the_script_uses_docker_exec_not_compose():
    """cron has no working directory; docker compose exec needs the repo."""
    assert "docker exec" in TEXT
    assert "docker compose exec" not in TEXT


def test_the_script_prints_no_secrets():
    for forbidden in ("cat .env", "printenv\n", "WGER_API_TOKEN",
                      "SESSION_SECRET", "AUTH_PASSWORD_HASH", "env\n"):
        assert forbidden not in TEXT


def test_the_backup_uses_the_sqlite_backup_api():
    """cp of a live WAL database catches a transaction mid-flight."""
    assert "src.backup(dst)" in TEXT
    assert "cp " not in TEXT.split("main()")[-1]


def test_the_backup_is_verified_before_rotation():
    assert "integrity_check" in TEXT
    assert TEXT.index("integrity_check") < TEXT.index("-delete")


def test_a_stopped_container_is_a_loud_failure():
    """A backup that quietly did not happen is worse than one that failed."""
    assert "ist nicht erreichbar" in TEXT
    assert "exit 1" in TEXT


# ---------------------------------------------------------------------------
# What rotation is allowed to touch
# ---------------------------------------------------------------------------

def _matches(name: str) -> bool:
    result = subprocess.run(
        ["bash", "-c",
         f'source "{SCRIPT}"; backup_name_matches "$1" && echo yes || echo no',
         "_", name],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "yes"


@needs_bash
def test_an_automatic_backup_matches():
    assert _matches("auto-backup-2026-08-18_03-30-00.sqlite")


@needs_bash
@pytest.mark.parametrize("name", [
    "wger_hero.sqlite",              # the live database
    "wger_hero.sqlite-wal",          # its write-ahead log
    "wger_hero.sqlite-shm",          # its shared memory file
    "wger_hero.db",                  # the repository default name
    "backup-2026-08-18_03-30-00.sqlite",          # a deployment backup
    "offline-2026-08-18_03-30-00.sqlite",         # the offline deploy backup
    "migrationstest-2026-08-18_03-30-00.sqlite",  # a migration rehearsal
    "vor-starter-2026-08-18_03-30-00.sqlite",     # taken before the campaign
    "vor-reparatur-2026-08-18_03-30-00.sqlite",   # taken before the repair
    "fehlgeschlagen-2026-08-18_03-30-00.sqlite",  # a forensic copy, never delete
    "auto-backup-.sqlite",                        # no timestamp
    "auto-backup-2026-08-18.sqlite",              # no time part
    "auto-backup-2026-08-18_03-30-00.sqlite.bak", # suffixed
    "auto-backup-xxxx-xx-xx_xx-xx-xx.sqlite",     # not digits
])
def test_everything_else_is_left_alone(name):
    assert not _matches(name)


def test_the_rotation_find_is_anchored():
    line = [l for l in TEXT.splitlines() if "-mtime" in l or "-mindepth" in l]
    rotation = "\n".join(
        TEXT.split("removed=\"$(find")[1].split(")\"")[0].splitlines()
    )
    for guard in ("-mindepth 1", "-maxdepth 1", "-type f", "-name", "-mtime"):
        assert guard in rotation, f"rotation is missing {guard}"
    assert rotation.index("-print") < rotation.index("-delete")
    assert line


def test_no_blanket_deletion_anywhere():
    assert "rm -rf" not in TEXT
    assert "rm -f *" not in TEXT
    # The only rm removes a file this run just created and named itself.
    assert TEXT.count("rm -f") == 1
    assert 'rm -f "$target_on_host"' in TEXT


# ---------------------------------------------------------------------------
# Rotation against real files
# ---------------------------------------------------------------------------

@needs_bash
def test_rotation_removes_only_aged_automatic_backups(tmp_path):
    old = time.time() - 60 * 60 * 24 * 40      # 40 days
    keep = ["auto-backup-2026-08-18_03-30-00.sqlite"]
    drop = ["auto-backup-2026-01-01_03-30-00.sqlite",
            "auto-backup-2026-01-02_03-30-00.sqlite"]
    protect = ["wger_hero.sqlite", "wger_hero.sqlite-wal", "wger_hero.sqlite-shm",
               "backup-2026-01-01_03-30-00.sqlite",
               "offline-2026-01-01_03-30-00.sqlite",
               "fehlgeschlagen-2026-01-01_03-30-00.sqlite"]

    for name in drop + protect:
        path = tmp_path / name
        path.write_text("x")
        os.utime(path, (old, old))
    for name in keep:
        (tmp_path / name).write_text("x")

    # A nested copy proves -maxdepth actually stops recursion.
    nested = tmp_path / "unterordner"
    nested.mkdir()
    nested_file = nested / "auto-backup-2026-01-03_03-30-00.sqlite"
    nested_file.write_text("x")
    os.utime(nested_file, (old, old))

    subprocess.run(
        ["find", str(tmp_path), "-mindepth", "1", "-maxdepth", "1", "-type", "f",
         "-name", ROTATION_GLOB, "-mtime", "+14", "-print", "-delete"],
        check=True, capture_output=True,
    )

    for name in drop:
        assert not (tmp_path / name).exists(), f"{name} should have been rotated"
    for name in keep + protect:
        assert (tmp_path / name).exists(), f"{name} must never be deleted"
    assert nested_file.exists(), "-maxdepth must stop rotation at the top level"


@needs_bash
def test_rotation_keeps_everything_inside_the_window(tmp_path):
    for name in ("auto-backup-2026-08-17_03-30-00.sqlite",
                 "auto-backup-2026-08-18_03-30-00.sqlite"):
        (tmp_path / name).write_text("x")

    subprocess.run(
        ["find", str(tmp_path), "-mindepth", "1", "-maxdepth", "1", "-type", "f",
         "-name", ROTATION_GLOB, "-mtime", "+14", "-print", "-delete"],
        check=True, capture_output=True,
    )

    assert len(list(tmp_path.iterdir())) == 2


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

def test_the_script_is_documented():
    deploy = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()
    assert "scripts/backup.sh" in deploy
    assert "crontab" in deploy.lower()
    assert "KEEP_DAYS" in deploy

    readme = (REPO_ROOT / "README.md").read_text()
    assert "scripts/backup.sh" in readme


def test_the_docs_say_it_does_not_replace_the_deploy_backup():
    deploy = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()
    section = deploy.split("Automatische Sicherung")[1]
    assert "ersetzt" in section
