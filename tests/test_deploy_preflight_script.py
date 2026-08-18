"""The preflight must catch the one thing that took production down.

An extra compose file exists but is not in the compose invocation. Every other
signal looks fine — the secrets are on disk, the repository is clean, the
container is running — and the next `up` recreates it without the auth mounts.
The preflight's job is to say so *before* anything is touched, and to say it
without ever printing a secret.

It reads only. These tests run it for real against temporary directories, which
is safe precisely because it creates and changes nothing.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_preflight.sh"

READY, DO_NOT_DEPLOY = 0, 2


def run(cwd: Path, **env) -> subprocess.CompletedProcess:
    """Run the preflight with a stub `docker` on PATH, never the real one."""
    environ = {**os.environ, "PATH": f"{cwd}/bin:{os.environ['PATH']}", **env}
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=cwd, env=environ,
        capture_output=True, text=True,
    )


@pytest.fixture
def install(tmp_path: Path) -> Path:
    """A miniature installation: a git repo, a compose file, both secrets."""
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "docker"
    # Echoes a compose config that mounts both secrets.
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "compose" ]; then\n'
        '  for a in "$@"; do [ "$a" = "config" ] && {\n'
        '    echo "    volumes:"\n'
        '    echo "      - target: /run/secrets/hero_password_hash"\n'
        '    echo "      - target: /run/secrets/hero_session_secret"\n'
        "    exit 0; }\n"
        "  done\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    (tmp_path / "docker-compose.yml").write_text("services:\n  wger-hero:\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "hero_password_hash").write_text("$argon2id$fake")
    (tmp_path / "secrets" / "hero_session_secret").write_text("deadbeef")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.st", "-c", "user.name=T", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Static properties
# ---------------------------------------------------------------------------

def test_the_script_is_syntactically_valid():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_the_script_only_reads():
    """No creation, no deletion, no container start. The whole point."""
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("rm ", "mv ", "touch ", "mkdir ", "install -d",
                      "openssl rand", "docker compose up", "docker compose run",
                      "docker start", "docker restart", ">>", "tee "):
        assert forbidden not in code, f"preflight would write: {forbidden!r}"


def test_the_script_never_prints_a_secret():
    body = SCRIPT.read_text()
    assert "cat " not in body
    assert "docker exec" not in body
    # Only metadata about the files.
    assert "stat -c" in body


def test_the_script_does_not_dump_the_whole_compose_config():
    """`compose config` resolves the service environment and would show .env."""
    body = SCRIPT.read_text()
    assert "grep -qF" in body
    assert "echo \"$config\"" not in body


def test_the_script_surveys_instead_of_aborting_early():
    """Unlike backup.sh: a preflight reports every finding in one pass."""
    body = SCRIPT.read_text()
    assert "set -uo pipefail" in body
    assert "set -euo pipefail" not in body


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

def test_a_healthy_installation_is_ready(install):
    result = run(install)
    assert result.returncode == READY
    assert "0 Fehler" in result.stdout


def test_an_extra_compose_file_outside_the_stack_blocks_the_deploy(install):
    """The exact production failure: the file exists, nobody passes -f."""
    (install / "docker-compose.auth.yml").write_text("services:\n  wger-hero:\n")
    result = run(install)

    assert result.returncode == DO_NOT_DEPLOY
    assert "docker-compose.auth.yml" in result.stdout
    assert "NICHT DEPLOYEN" in result.stdout


def test_the_same_file_inside_the_stack_is_fine(install):
    (install / "docker-compose.auth.yml").write_text("services:\n  wger-hero:\n")
    result = run(
        install,
        DC="docker compose -f docker-compose.yml -f docker-compose.auth.yml",
    )
    assert result.returncode == READY
    assert "explizit in DC" in result.stdout


def test_the_override_file_needs_no_mention(install):
    """Compose loads it by itself, so requiring it in DC would be wrong."""
    (install / "docker-compose.override.yml").write_text("services:\n  wger-hero:\n")
    result = run(install)
    assert result.returncode == READY


def test_an_unmounted_secret_blocks_the_deploy(install):
    """Secrets present on disk, but the effective config does not mount them —
    the container would come up returning 503."""
    stub = install / "bin" / "docker"
    stub.write_text("#!/usr/bin/env bash\necho 'volumes: []'\nexit 0\n")
    stub.chmod(0o755)

    result = run(install)
    assert result.returncode == DO_NOT_DEPLOY
    assert "/run/secrets/hero_session_secret" in result.stdout


def test_an_empty_secret_file_blocks_the_deploy(install):
    (install / "secrets" / "hero_session_secret").write_text("")
    result = run(install)
    assert result.returncode == DO_NOT_DEPLOY
    assert "LEER" in result.stdout


def test_a_missing_secret_is_only_a_note(install):
    """AUTH_ENABLED=false is a legitimate local setup; the preflight must not
    pretend to know the .env it is told not to read."""
    (install / "secrets" / "hero_session_secret").unlink()
    result = run(install)
    assert result.returncode == READY
    assert "hero_session_secret fehlt" in result.stdout


def test_a_dirty_working_tree_is_reported_but_not_fatal(install):
    (install / "docker-compose.yml").write_text("services:\n  wger-hero:\n    x: 1\n")
    result = run(install)
    assert result.returncode == READY
    assert "lokale Änderungen" in result.stdout


def test_missing_docker_does_not_crash_the_preflight(install, tmp_path):
    """Reporting the other three checks still beats reporting nothing."""
    (install / "bin" / "docker").unlink()
    result = run(install)
    assert result.returncode == READY
    assert "ungeprüft" in result.stdout


def test_the_output_never_contains_the_secret_values(install):
    result = run(install)
    assert "$argon2id$fake" not in result.stdout
    assert "deadbeef" not in result.stdout


def test_it_changes_nothing_it_looked_at(install):
    """Belt and braces: compare the whole tree before and after."""
    def snapshot():
        return {
            p.relative_to(install): p.read_bytes()
            for p in sorted(install.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        }

    before = snapshot()
    run(install)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

def test_the_script_is_documented():
    deploy = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()
    assert "scripts/deploy_preflight.sh" in deploy
