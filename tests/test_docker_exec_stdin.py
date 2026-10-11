"""Every `docker exec` that feeds a heredoc passes -i.

Without it docker exec does not forward stdin. `python - <<'PY'` then reads
nothing, runs nothing and exits 0 — the step looks like a pass. scripts/backup.sh
never produced a backup that way, and two smoke checks never ran.

`docker compose exec` keeps stdin open by default, so DEPLOY.md's
`"${DC[@]}" exec ... <<'PY'` is not affected and not checked here.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO_ROOT / "scripts").glob("*.sh"))


def docker_exec_commands(text: str) -> list:
    """Each `docker exec` command with its continuation lines joined."""
    lines = text.splitlines()
    commands, n = [], 0
    while n < len(lines):
        line = lines[n]
        if re.search(r"(^|\s)docker exec\s", line) and not line.lstrip().startswith("#"):
            full = line
            while full.rstrip().endswith("\\") and n + 1 < len(lines):
                n += 1
                full = full.rstrip()[:-1] + " " + lines[n].strip()
            commands.append(full.strip())
        n += 1
    return commands


def feeds_a_heredoc(command: str) -> bool:
    return bool(re.search(r"<<-?\s*'?[A-Z]+'?", command))


def passes_stdin(command: str) -> bool:
    return bool(re.search(r"docker exec\s+(?:-[a-zA-Z]+\s+(?:\S+\s+)?)*?-(?:i|it|ti)\b", command))


def heredoc_calls() -> list:
    return [
        (script.name, command)
        for script in SCRIPTS
        for command in docker_exec_commands(script.read_text())
        if feeds_a_heredoc(command)
    ]


def test_the_checker_reads_the_flag_where_it_stands():
    assert passes_stdin("docker exec -i \"$C\" python - <<'PY'")
    assert passes_stdin("docker exec -i -e A=\"$X\" \"$C\" python - <<'PY'")
    assert not passes_stdin("docker exec -e SRC=\"$DB\" \"$C\" python - <<'PY'")
    assert not passes_stdin("docker exec \"$C\" python - <<'PY'")


def test_the_known_heredoc_calls_are_all_found():
    """Guards the guard: a parser that finds nothing would pass anything."""
    found = {name for name, _ in heredoc_calls()}
    assert {"backup.sh", "smoke_full.sh", "smoke_today_week.sh"} <= found


def test_every_heredoc_reaches_the_container():
    missing = [f"{name}: {command}" for name, command in heredoc_calls() if not passes_stdin(command)]
    assert missing == []
