"""The image ships the tools the documented commands call inside it.

docs/DEPLOY.md and the smoke scripts run `sqlite3` through `docker exec` — for
the integrity check, the pre-deploy backup and every read-only count after it.
python:3.12-slim carries the SQLite library Python links against, not the
command-line tool, so until the Dockerfile installed it each of those steps
failed with "executable file not found in $PATH".

No docker here: these read the Dockerfile and the documents as text.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup.sh"

# `docker exec "$C" sqlite3`, `docker compose exec wger-hero sqlite3` and
# `"${DC[@]}" exec wger-hero sqlite3` — the CLI run inside the container.
IN_CONTAINER_CLI = re.compile(r"\bexec\b[^\n|;&]*?\ssqlite3\s")


def apt_packages(dockerfile: str) -> set:
    """Every package an `apt-get install` in the Dockerfile names."""
    joined = dockerfile.replace("\\\n", " ")
    packages = set()
    for line in joined.splitlines():
        match = re.search(r"apt-get install\s+(.*?)(?:&&|$)", line)
        if match:
            packages.update(t for t in match.group(1).split() if not t.startswith("-"))
    return packages


def files_calling_the_cli_in_the_container() -> list:
    candidates = [
        REPO_ROOT / "README.md",
        *sorted((REPO_ROOT / "docs").glob("*.md")),
        *sorted((REPO_ROOT / "scripts").glob("*.sh")),
    ]
    return [
        p.relative_to(REPO_ROOT).as_posix()
        for p in candidates
        if IN_CONTAINER_CLI.search(p.read_text())
    ]


def code_lines(script: str) -> list:
    return [line for line in script.splitlines() if not line.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# The parser this file relies on
# ---------------------------------------------------------------------------

def test_the_package_parser_reads_a_continued_install_line():
    text = "RUN apt-get update && apt-get install -y --no-install-recommends \\\n    gcc \\\n    sqlite3 \\\n    && rm -rf /var/lib/apt/lists/*\n"
    assert apt_packages(text) == {"gcc", "sqlite3"}


def test_the_cli_pattern_tells_the_cli_from_the_python_module():
    assert IN_CONTAINER_CLI.search('"${DC[@]}" exec wger-hero sqlite3 -readonly "$DB" \\\n')
    assert IN_CONTAINER_CLI.search('docker exec "$CONTAINER" sqlite3 -readonly "$DB" \\\n')
    assert not IN_CONTAINER_CLI.search("docker exec \"$CONTAINER\" python - <<'PY'\n")
    assert not IN_CONTAINER_CLI.search("import sqlite3\n")


# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------

def test_the_documents_do_call_the_cli_inside_the_container():
    """Guards the guard: should this ever come back empty, the install below
    has lost its reason and this file needs rethinking, not deleting."""
    files = files_calling_the_cli_in_the_container()
    assert "docs/DEPLOY.md" in files
    assert "scripts/smoke_full.sh" in files


def test_the_image_installs_the_sqlite3_cli():
    assert "sqlite3" in apt_packages(DOCKERFILE.read_text())


def test_the_install_keeps_the_image_lean():
    text = DOCKERFILE.read_text()
    assert "--no-install-recommends" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


# ---------------------------------------------------------------------------
# The automatic backup stays independent of it
# ---------------------------------------------------------------------------

def test_the_automatic_backup_does_not_need_the_cli():
    """Cron runs backup.sh against whatever image is deployed — including one
    rolled back to before the CLI was added (DEPLOY.md 19a). It therefore keeps
    using Python's own sqlite3 module and never calls the CLI."""
    script = BACKUP_SCRIPT.read_text()
    assert not any(IN_CONTAINER_CLI.search(line + "\n") for line in code_lines(script))
    assert "src.backup(dst)" in script


def test_the_backup_script_no_longer_claims_the_image_lacks_the_cli():
    assert "which the\n    # image does not install" not in BACKUP_SCRIPT.read_text()


# ---------------------------------------------------------------------------
# The first update onto such an image
# ---------------------------------------------------------------------------

def test_the_deploy_doc_covers_the_first_update_from_an_image_without_the_cli():
    """Sections 3, 4 and 7 exec into the container that is still running —
    the OLD image. On the update that introduces the CLI it is not there yet."""
    deploy = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()
    section_3 = deploy.index("## 3. SQLite-Integrität prüfen")
    section_4 = deploy.index("## 4. Konsistentes Online-Backup erstellen")
    note = deploy.index("command -v sqlite3")
    assert section_3 < note < section_4
