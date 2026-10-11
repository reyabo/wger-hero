"""CLAUDE.md points at the database path instead of stating one.

It used to name /srv/data/wger-hero/wger_hero.db, a file the server does not
have — the live database there is wger_hero.sqlite, and the repository default
says .db. docs/DEPLOY.md section 0 is the one place that settles the path, so
CLAUDE.md refers to it, and these tests keep that reference from dangling.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = (REPO_ROOT / "CLAUDE.md").read_text()
DEPLOY = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()

SUBSECTION = "Den Datenbankpfad zuerst feststellen"


def test_claude_md_states_no_database_file_path():
    assert not re.search(r"/(srv/data/wger-hero|data)/wger_hero\.(db|sqlite)", CLAUDE_MD)


def test_claude_md_refers_to_deploy_section_0():
    assert "docs/DEPLOY.md" in CLAUDE_MD
    assert "Abschnitt 0" in CLAUDE_MD
    assert SUBSECTION in CLAUDE_MD


def test_the_referenced_subsection_is_in_section_0():
    start = DEPLOY.index("## 0. ")
    end = DEPLOY.index("## 1. ")
    assert start < DEPLOY.index(f"### {SUBSECTION}") < end
