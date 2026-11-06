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


def _python_files():
    for path in sorted((ROOT / "silica").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        yield str(path.relative_to(ROOT)), source, ast.parse(source)


def _is_environ(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" \
        and isinstance(node.value, ast.Name) and node.value.id == "os"


def test_nothing_outside_config_reads_the_environment():
    """A key read at its call site is one the table cannot see, with a default and
    a spelling of true of its own: 87 such reads in 41 files before the table."""
    # The package root captures the shell before anything can blur it (ADR-0073).
    own = {"silica/config.py", "silica/__init__.py"}
    found = []
    for rel, _source, tree in _python_files():
        if rel in own:
            continue
        for node in ast.walk(tree):
            getenv = isinstance(node, ast.Attribute) and node.attr == "getenv" \
                and isinstance(node.value, ast.Name) and node.value.id == "os"
            get = isinstance(node, ast.Attribute) and node.attr == "get" and _is_environ(node.value)
            item = isinstance(node, ast.Subscript) and _is_environ(node.value) and isinstance(node.ctx, ast.Load)
            if getenv or get or item:
                found.append(f"{rel}:{node.lineno}")
    assert found == []


def test_a_live_key_is_only_written_through_claim_env():
    """drop_foreign_env takes back whatever silica did not claim (B6). keyphrase's
    hand-run self-test writes its own process's knobs and never reaches a model call."""
    found = []
    for rel, _source, tree in _python_files():
        if rel in {"silica/config.py", "silica/__init__.py", "silica/kernel/text/keyphrase.py"}:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_environ(node.value) and isinstance(node.ctx, ast.Store):
                found.append(f"{rel}:{node.lineno}")
    assert found == []


def test_every_key_the_code_names_is_in_the_table():
    shape = re.compile(r"(SILICA|OPENROUTER|DISTILLER|ANNEAL|OLLAMA|WIKI)_[A-Z0-9_]*[A-Z0-9]|MAX_TOKENS|MODEL_CONTEXT_WINDOW")
    found = set()
    for rel, _source, tree in _python_files():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and shape.fullmatch(node.value):
                if node.value not in NAMES:
                    found.add(f"{rel}: {node.value}")
    assert sorted(found) == []


def test_env_example_ships_the_live_thresholds():
    """H20: it shipped the Qwen-era bars while the default embedder ran its own."""
    from silica.config import DEFAULT_EMBEDDING_MODEL, EMBEDDER_DEFAULTS

    text = (ROOT / ".env.example").read_text()
    row = EMBEDDER_DEFAULTS[DEFAULT_EMBEDDING_MODEL]
    for key in ("SILICA_SIM_THRESHOLD_HIGH", "SILICA_SIM_THRESHOLD_LOW",
                "SILICA_SIM_TITLE_THRESHOLD", "SILICA_SIM_THRESHOLD_THEME"):
        assert f"# {key}={row[key]}\n" in text


def test_a_strict_flag_takes_one_alone_and_a_kill_switch_only_zero(monkeypatch):
    """The two rules that are not `true_word`, by key, because each has a reader
    relying on it: `true` must not turn hosted on, `yes` must not turn a check off."""
    for value, on in (("1", True), ("true", False), ("t", False), ("yes", False), ("", False)):
        monkeypatch.setenv("SILICA_HOSTED", value)
        assert live_env("SILICA_HOSTED") is on
    for value, on in (("0", False), ("1", True), ("false", True), ("yes", True), ("", True)):
        monkeypatch.setenv("SILICA_META_SHAPE_CHECK", value)
        assert live_env("SILICA_META_SHAPE_CHECK") is on


def test_an_alias_answers_when_the_new_name_is_unset_or_empty(monkeypatch):
    for key in ("SILICA_STT_BASE_URL", "SILICA_ASR_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert SilicaConfig().stt_base_url == "http://localhost:1236/v1"
    monkeypatch.setenv("SILICA_ASR_BASE_URL", "http://old/v1")
    assert SilicaConfig().stt_base_url == "http://old/v1"
    monkeypatch.setenv("SILICA_STT_BASE_URL", "")
    assert SilicaConfig().stt_base_url == "http://old/v1"
    monkeypatch.setenv("SILICA_STT_BASE_URL", "http://new/v1")
    assert SilicaConfig().stt_base_url == "http://new/v1"


def test_the_legacy_names_stay_readable():
    aliases = {a for row in SETTINGS.values() for a in row.aliases}
    assert {f"SILICA_ASR_{n}" for n in ("BASE_URL", "MODEL", "LANG", "PROVIDER", "WHISPERCPP_BIN", "WHISPERCPP_MODEL")} <= aliases
    assert {"SILICA_EPISODIC_NUCLEATION_RUNS", "SILICA_YAKE_POOL", "TAVILY_API_KEY", "OPENROUTER_PROVIDER"} <= aliases
