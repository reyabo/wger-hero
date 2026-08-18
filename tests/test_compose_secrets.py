"""Every secret file the configuration points at must actually be mounted.

This is the failure that took the production instance down: `.env.example`
declared `SESSION_SECRET_FILE=/run/secrets/hero_session_secret`, the access
middleware read it on every request, and `docker-compose.yml` never mounted it.
The container came up, the healthcheck went red and every path — including
`/login` and `/healthz` — answered 503 "Auth not configured".

Nothing in the test suite could see that, because the suite never looks at the
deployment. These tests close exactly that gap: they compare the two files
against each other, so a future secret cannot be introduced in one and
forgotten in the other.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# `NAME_FILE=/run/secrets/x` in .env.example — a path inside the container that
# the app will try to read.
_DECLARED = re.compile(r"^([A-Z_]+_FILE)=(/run/secrets/\S+)", re.M)
# `- ./host/path:/run/secrets/x:ro` in the compose volume list.
_MOUNTED = re.compile(r"-\s*\S+:(/run/secrets/\S+?):ro")


def declared_secret_paths() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _DECLARED.finditer(ENV_EXAMPLE.read_text())}


def mounted_secret_paths() -> set[str]:
    return set(_MOUNTED.findall(COMPOSE.read_text()))


def test_the_example_env_declares_secret_files():
    """Guard the guard: if this ever returns nothing the checks below pass
    vacuously and would stop protecting anything."""
    assert declared_secret_paths()


def test_the_compose_file_mounts_secrets():
    assert mounted_secret_paths()


@pytest.mark.parametrize(
    "variable,path",
    sorted(declared_secret_paths().items()),
    ids=lambda v: v if isinstance(v, str) and v.isupper() else str(v),
)
def test_every_declared_secret_is_mounted(variable, path):
    assert path in mounted_secret_paths(), (
        f"{variable}={path} is declared in .env.example but docker-compose.yml "
        f"never mounts it — the container would answer 503 on every request"
    )


def test_the_auth_secrets_are_mounted_by_name():
    """Named explicitly, so deleting the parametrised check above is not enough
    to lose the coverage that actually mattered."""
    mounted = mounted_secret_paths()
    assert "/run/secrets/hero_session_secret" in mounted
    assert "/run/secrets/hero_password_hash" in mounted


def test_secrets_are_mounted_read_only():
    """The app only ever reads them, and a writable mount would let a bug
    destroy the password hash."""
    for line in COMPOSE.read_text().splitlines():
        if "/run/secrets/" in line:
            assert line.rstrip().endswith(":ro"), f"not read-only: {line.strip()}"


def test_no_secret_value_is_committed():
    """The mounts point at files; the files themselves must stay out of git."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    assert "secrets/" in gitignore
    for host_path in re.findall(r"-\s*(\S+):/run/secrets/", COMPOSE.read_text()):
        assert host_path.startswith("./secrets/"), (
            f"{host_path} is outside the git-ignored secrets/ directory"
        )
        assert not (REPO_ROOT / host_path.lstrip("./")).exists(), (
            f"{host_path} exists in the working tree — a real secret must never "
            f"be committed"
        )
