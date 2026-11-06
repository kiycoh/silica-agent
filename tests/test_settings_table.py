"""One setting is declared in one place: `silica.config.SETTINGS`. What else
names a key, `.env.example`, the Settings panel and the doctor, is held to that
table here, so a key added to one of them alone fails instead of drifting."""
from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

from silica.config import LIVE, SETTINGS, SilicaConfig, live_env

ROOT = Path(__file__).resolve().parent.parent
NAMES = {name for row in SETTINGS.values() for name in (row.key, *row.aliases)}
# Runtime state of the REPL, and what outranks the embedder row: not settings.
NOT_SETTINGS = {"context_tokens", "_pinned"}


def _example_keys() -> set[str]:
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", (ROOT / ".env.example").read_text(), re.M))


def test_every_config_field_has_a_row():
    bare = [f.name for f in dataclasses.fields(SilicaConfig)
            if "setting" not in f.metadata and f.name not in NOT_SETTINGS]
    assert bare == []


def test_a_key_has_one_home():
    assert not set(LIVE) & {r.key for r in SETTINGS.values() if r.field}


def test_env_example_and_the_table_agree_both_ways():
    # A provider key's numbered siblings are a pattern of its row (model.keys).
    example = {k for k in _example_keys() if not re.fullmatch(r".+_API_KEY_\d+", k)}
    assert sorted(example - NAMES) == []
    assert sorted(example ^ {r.key for r in SETTINGS.values() if r.doc}) == []


def test_panel_rows_and_the_table_agree_both_ways():
    from silica.ui.web import settings as panel

    rows = {r.key: r for section in panel.sections().values() for r in section
            if r.kind != "readonly" and r.key != panel.settings_ops.SAFE_MODE_KEY}
    assert sorted(set(rows) ^ {r.key for r in SETTINGS.values() if r.panel}) == []
    for key, row in rows.items():
        # `_provider` behind the `provider` property, the one the panel assigns.
        assert row.attr == SETTINGS[key].field.lstrip("_"), key


def test_the_doctor_names_only_keys_that_exist():
    text = (ROOT / "silica/onboarding/checks.py").read_text()
    assert sorted(set(re.findall(r"\bSILICA_[A-Z][A-Z0-9_]+", text)) - NAMES) == []


def test_live_env_refuses_a_key_with_no_row():
    import pytest

    with pytest.raises(KeyError):
        live_env("SILICA_NOT_A_SETTING")


def test_no_read_of_a_setting_falls_back_to_a_default_of_its_own():
    """`getattr(CONFIG, "x", d)` on a class that always has `x` only ever answers
    when `x` was renamed, and then with a stale value, in silence."""
    receivers = {"CONFIG", "config", "cfg", "self.config", "orch.CONFIG", "_CFG"}
    found = []
    for path in (ROOT / "silica").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr"
                    and len(node.args) == 3 and ast.get_source_segment(source, node.args[0]) in receivers):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert found == []
