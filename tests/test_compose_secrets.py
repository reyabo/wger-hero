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


def test_local_compose_overlays_are_git_ignored():
    """A production install keeps its secret mounts in an extra compose file.
    That file names host paths of one specific machine and must never be
    committed — and it must not be committed by accident either, which is what
    .gitignore is for."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    for name in ("docker-compose.override.yml", "docker-compose.auth.yml"):
        assert name in gitignore, f"{name} is not git-ignored"


def test_no_local_compose_overlay_is_committed():
    tracked = {p.name for p in REPO_ROOT.glob("docker-compose*.yml")}
    assert tracked == {"docker-compose.yml"}, (
        f"unexpected compose files in the repository: {sorted(tracked)}"
    )


# ---------------------------------------------------------------------------
# FitTrackee: a read-only secret and a writable token store, never confused
# ---------------------------------------------------------------------------

def test_the_fittrackee_client_secret_is_mounted_read_only():
    compose = COMPOSE.read_text()
    assert "/run/secrets/fittrackee_client_secret:ro" in compose


def test_the_fittrackee_token_store_is_writable():
    """Tokens are rewritten on every refresh, so this one mount must not be
    read-only — a :ro token store would break the refresh instead of the
    credential, which is a far more confusing failure."""
    compose = COMPOSE.read_text()
    line = next(
        l for l in compose.splitlines() if "wger-hero-fittrackee" in l and l.strip().startswith("-")
    )
    assert line.rstrip().endswith(":rw")


def test_the_token_store_is_not_inside_the_secrets_directory():
    """A token write must never be able to reach the client secret."""
    compose = COMPOSE.read_text()
    token_mounts = [
        l for l in compose.splitlines()
        if "wger-hero-fittrackee" in l and l.strip().startswith("-")
    ]
    assert token_mounts
    for line in token_mounts:
        target = line.split(":")[-2]
        assert not target.startswith("/run/secrets")


def test_the_env_example_documents_the_fittrackee_paths():
    env = ENV_EXAMPLE.read_text()
    assert "FITTRACKEE_CLIENT_SECRET_FILE=/run/secrets/fittrackee_client_secret" in env
    assert "FITTRACKEE_TOKEN_DIR=/run/wger-hero-fittrackee" in env


def test_the_declared_fittrackee_paths_match_the_mounts():
    """The exact drift that took auth down, now checked for FitTrackee too."""
    env = ENV_EXAMPLE.read_text()
    compose = COMPOSE.read_text()

    secret = re.search(r"FITTRACKEE_CLIENT_SECRET_FILE=(\S+)", env).group(1)
    token_dir = re.search(r"FITTRACKEE_TOKEN_DIR=(\S+)", env).group(1)

    assert f":{secret}:ro" in compose
    assert f":{token_dir}:rw" in compose


def test_the_env_example_never_holds_the_client_secret():
    """Only a path may appear here — never a value."""
    env = ENV_EXAMPLE.read_text()
    assert not re.search(r"^FITTRACKEE_CLIENT_SECRET=", env, re.M)


def test_the_readme_documents_the_same_paths():
    readme = (REPO_ROOT / "README.md").read_text()
    assert "/run/secrets/fittrackee_client_secret" in readme
    assert "/run/wger-hero-fittrackee" in readme


def test_no_fittrackee_secret_is_committed():
    for path in ("secrets/fittrackee/client_secret", "secrets/fittrackee-oauth/tokens.json"):
        assert not (REPO_ROOT / path).exists(), f"{path} must never be in the repository"
