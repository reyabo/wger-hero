"""The update procedure must not talk a working installation into a 503.

Two defects produced the outage this guards against. First, `docker-compose.yml`
declared no auth mounts at all (covered by test_compose_secrets.py). Second — and
this one survives that fix — a production install can keep its mounts in an
extra compose file, and a bare `docker compose up -d --build` silently drops it:
the container is recreated without the mounts while the secrets sit untouched on
disk. The documentation is the only place that can prevent the second one, so
these tests treat it as code.

They also pin the safety rule that matters more than either: never recreate
secrets to fix a missing mount. A new password hash changes the login password
and a new session secret logs every session out, for a problem neither one
solves.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
DEPLOY = REPO_ROOT / "docs" / "DEPLOY.md"

SECRET_BASENAMES = ("hero_password_hash", "hero_session_secret")


def _bash_lines(text: str) -> list[str]:
    """Lines inside ```bash fences — the only lines a reader will run.

    Prose may name a wrong command (`docker compose up -d --build`) precisely in
    order to warn about it; that must not count as recommending it.
    """
    lines, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = stripped in ("```bash", "```sh")
            continue
        if inside and stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def _section(text: str, heading: str) -> str:
    """The body of one markdown section, up to the next heading of its level."""
    level = heading.split(" ", 1)[0]
    start = text.index(heading)
    nxt = re.compile(rf"^{re.escape(level)} ", re.M)
    m = nxt.search(text, start + len(heading))
    return text[start : m.start() if m else len(text)]


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text()


@pytest.fixture(scope="module")
def deploy() -> str:
    return DEPLOY.read_text()


# ---------------------------------------------------------------------------
# Extra compose files must survive an update
# ---------------------------------------------------------------------------

def test_the_update_section_exists(readme):
    """Guard the guard — the checks below select this section by name."""
    assert "## Updating a running instance" in readme


def test_the_update_section_warns_about_extra_compose_files(readme):
    section = _section(readme, "## Updating a running instance")
    assert "-f" in section
    assert "docker-compose.override.yml" in section
    lowered = section.lower()
    assert "compose loads" in lowered or "compose lädt" in lowered


def test_the_warning_comes_before_the_bare_update_command(readme):
    """A reader who stops at the first command block must already have been
    told that their extra -f files are needed."""
    section = _section(readme, "## Updating a running instance")
    warning = section.index("-f")
    bare = section.index("docker compose up -d --build")
    assert warning < bare, "the bare update command appears before the warning"


def test_the_update_section_shows_a_full_compose_stack(readme):
    """Not just a warning — a usable command for the case it warns about."""
    section = _section(readme, "## Updating a running instance")
    assert "DC=(docker compose" in section
    assert '"${DC[@]}"' in section


def test_the_bare_command_is_not_the_only_recipe(readme):
    """`docker compose up -d --build` stays legitimate for a plain install; it
    must not be the sole instruction offered to everyone."""
    section = _section(readme, "## Updating a running instance")
    assert section.count('"${DC[@]}"') >= 3


def test_the_deploy_procedure_never_calls_compose_bare(deploy):
    """DEPLOY.md is the production procedure. Every compose call there goes
    through the pinned stack, or an install with extra files loses them."""
    offenders = [
        line for line in _bash_lines(deploy)
        if re.search(r"\bdocker compose\b", line) and not line.startswith("DC=(")
    ]
    assert not offenders, f"bare compose calls in DEPLOY.md: {offenders}"


def test_the_deploy_procedure_defines_the_stack_up_front(deploy):
    assert "DC=(docker compose" in deploy
    assert deploy.index("DC=(docker compose") < deploy.index("## 1.")


def test_the_deploy_procedure_checks_the_mounts_before_recreating(deploy):
    """The check is worthless after the container is already gone."""
    check = deploy.index("/run/secrets/hero_")
    recreate = deploy.index('"${DC[@]}" up -d')
    assert check < recreate


# ---------------------------------------------------------------------------
# Never replace a working credential to fix a missing mount
# ---------------------------------------------------------------------------

def test_the_update_section_does_not_create_secrets(readme):
    """Secret creation belongs in the recovery section, never in the routine
    update — that is how a password gets replaced by accident."""
    section = _section(readme, "## Updating a running instance")
    assert "openssl rand" not in section
    assert "PasswordHasher" not in section


def test_there_is_a_separate_recovery_section(readme):
    assert "## Restoring missing auth secrets" in readme


def test_the_recovery_section_checks_before_it_creates(readme):
    section = _section(readme, "## Restoring missing auth secrets")
    assert section.index("config | grep") < section.index("openssl rand")


def test_the_recovery_section_names_the_cost_of_replacing(readme):
    section = _section(readme, "## Restoring missing auth secrets").lower()
    assert "invalidates existing sessions" in section
    assert "changes the login password" in section


@pytest.mark.parametrize("name", SECRET_BASENAMES)
def test_every_secret_creation_is_guarded(readme, name):
    """`test ! -e` before every write, so a rerun cannot clobber a live
    credential."""
    section = _section(readme, "## Restoring missing auth secrets")
    assert f"test ! -e secrets/{name}" in section


@pytest.mark.parametrize("name", SECRET_BASENAMES)
def test_the_guard_aborts_rather_than_continues(readme, name):
    section = _section(readme, "## Restoring missing auth secrets")
    guard = section.index(f"test ! -e secrets/{name}")
    following = section[guard : guard + 300]
    assert "ABORT" in following
    assert "exit 1" in following


def test_the_docs_say_a_missing_mount_is_the_likely_cause(readme):
    section = _section(readme, "## Restoring missing auth secrets").lower()
    assert "missing `-f`" in section or "without the compose file" in section


# ---------------------------------------------------------------------------
# Never print a secret
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc", [README, DEPLOY], ids=lambda p: p.name)
def test_no_document_prints_a_secret(doc):
    text = doc.read_text()
    for name in SECRET_BASENAMES:
        for forbidden in (f"cat secrets/{name}", f"cat /run/secrets/{name}"):
            assert forbidden not in text, f"{doc.name} prints {name}"
    assert not re.search(r"docker (exec|compose exec)[^\n]*\bcat /run/secrets/", text)


@pytest.mark.parametrize("doc", [README, DEPLOY], ids=lambda p: p.name)
def test_no_document_writes_a_secret_in_clear_text(doc):
    """`echo "hunter2" > secrets/...` would put the password in the shell
    history and in the file. The password is only ever typed interactively."""
    text = doc.read_text()
    for name in SECRET_BASENAMES:
        assert not re.search(rf"echo\s+[^|\n]*>\s*\S*{name}", text)


def test_the_password_is_read_interactively(readme):
    section = _section(readme, "## Restoring missing auth secrets")
    assert "getpass" in section
    # Not as an argument, which would land in the shell history.
    assert not re.search(r"--password[= ]", section)


@pytest.mark.parametrize("doc", [README, DEPLOY], ids=lambda p: p.name)
def test_no_document_recommends_a_full_environment_dump(doc):
    """`printenv` without a variable name prints the API token too."""
    for line in _bash_lines(doc.read_text()):
        # A bare `env` / `printenv`, or one at the end of a pipeline. Naming a
        # single variable (`printenv DATABASE_URL`) is fine and stays allowed.
        assert not re.match(r"^(printenv|env)$", line), line
        assert not re.search(r"[|;&]\s*(printenv|env)\s*$", line), line


# ---------------------------------------------------------------------------
# The smoke test after an update
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,code", [("healthz", 200), ("login", 200), ("today", 303)])
def test_the_auth_smoke_test_pins_the_expected_codes(readme, path, code):
    section = _section(readme, "## Updating a running instance")
    assert f"/{path}" in section
    assert str(code) in section


def test_the_deploy_smoke_test_checks_the_mounts_are_read_only(deploy):
    section = _section(deploy, "## 12a. Zugriffsschutz wirkt")
    assert "mode=ro" in section
    assert ".Mounts" in section


def test_the_deploy_smoke_test_explains_a_wrong_success(deploy):
    """today=200 means auth is off, which looks like success and is not."""
    section = _section(deploy, "## 12a. Zugriffsschutz wirkt")
    assert "today=200" in section
