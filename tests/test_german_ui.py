"""The interface speaks German throughout.

Stored values stay English — recurrence, quest type, period, stat and category
keys are data, not text, and translating them would break every comparison in
the codebase. Only what the user reads is German, via label maps.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

# Words that would only ever appear as English interface text. Deliberately
# short and specific: a longer list would start flagging code and CSS classes.
ENGLISH_UI_WORDS = (
    "Save", "Cancel", "Delete", "Edit", "Complete", "Settings",
    "Active", "Inactive", "Completed", "Locked", "Unlocked",
)


def _visible_text(html: str) -> str:
    """Everything a browser would render, without markup, script or Jinja."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"\{\{.*?\}\}", " ", html, flags=re.S)
    html = re.sub(r"\{%.*?%\}", " ", html, flags=re.S)
    html = re.sub(r"\{#.*?#\}", " ", html, flags=re.S)
    html = re.sub(r"<[^>]+>", " ", html, flags=re.S)
    return html


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_no_english_interface_words_remain(path):
    text = _visible_text(path.read_text())
    found = [w for w in ENGLISH_UI_WORDS if re.search(rf"\b{w}\b", text)]
    assert not found, f"{path.name} still shows English UI text: {found}"


def test_the_navigation_is_german():
    base = (TEMPLATES / "base.html").read_text()
    assert ">Gewohnheiten</a>" in base
    assert ">Erfolge</a>" in base
    assert ">Habits</a>" not in base
    assert ">Achievements</a>" not in base


def test_stored_values_stay_english():
    """The label maps translate for display; the keys themselves must not move."""
    from app.habits import RECURRENCE_CHOICES, RECURRENCE_LABELS
    from app.quests import PERIOD_CHOICES, PERIOD_LABELS, QUEST_TYPE_CHOICES, QUEST_TYPE_LABELS

    assert set(RECURRENCE_LABELS) == set(RECURRENCE_CHOICES)
    assert set(PERIOD_LABELS) == set(PERIOD_CHOICES)
    assert set(QUEST_TYPE_LABELS) == set(QUEST_TYPE_CHOICES)
    for choices in (RECURRENCE_CHOICES, PERIOD_CHOICES, QUEST_TYPE_CHOICES):
        for key in choices:
            assert key.isascii() and key.islower()
