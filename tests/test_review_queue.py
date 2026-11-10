"""Tests for the Async Review Queue surface (Tier 1 Item 3 — ADR-0007).

Covers:
- DeferredStore.queue_depth()
- queue_depth emitted in ledger digest
- /review and /anneal registered in COMMANDS and dispatchable
"""
from __future__ import annotations

import pytest

from silica.kernel.write.deferred import DeferredStore


# ---------------------------------------------------------------------------
# DeferredStore.queue_depth
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return DeferredStore(path=tmp_path / "deferred")


def test_queue_depth_empty(store):
    assert store.queue_depth() == 0


def test_queue_depth_one_bundle(store):
    store.put("h1", "inbox/a.md", "Dir", None, [{"op": "write"}])
    assert store.queue_depth() == 1


def test_queue_depth_multiple_bundles(store):
    store.put("h1", "inbox/a.md", "Dir", None, [{"op": "write"}])
    store.put("h2", "inbox/b.md", "Dir", None, [{"op": "patch"}, {"op": "write"}])
    assert store.queue_depth() == 2


def test_queue_depth_decreases_after_remove(store):
    store.put("h1", "inbox/a.md", "Dir", None, [])
    store.put("h2", "inbox/b.md", "Dir", None, [])
    store.remove("h1")
    assert store.queue_depth() == 1


# ---------------------------------------------------------------------------
# queue_depth in ledger digest
# ---------------------------------------------------------------------------

def test_digest_includes_queue_depth_when_nonzero(tmp_path, monkeypatch):
    import silica.kernel.progress as prog_mod
    from silica.kernel.write.deferred import get_deferred_store

    prog_mod.runs_dir = lambda: tmp_path
    # conftest isolates the default store; populate it through the public seam.
    get_deferred_store().put("h1", "inbox/a.md", "Dir", None, [{"op": "write"}])

    from silica.kernel.progress import ProgressLedger
    p = ProgressLedger.new(mode="inject", inputs={})
    digest = p.digest()
    assert "review" in digest.lower() or "deferred" in digest.lower() or "queue" in digest.lower()


def test_digest_omits_queue_line_when_empty(tmp_path, monkeypatch):
    import silica.kernel.progress as prog_mod

    prog_mod.runs_dir = lambda: tmp_path

    from silica.kernel.progress import ProgressLedger
    p = ProgressLedger.new(mode="inject", inputs={})
    digest = p.digest()
    assert "REVIEW QUEUE" not in digest


# ---------------------------------------------------------------------------
# /review command registered in COMMANDS list
# ---------------------------------------------------------------------------

def test_review_command_in_commands_list():
    from silica.command_table import COMMANDS
    names = {c.name for c in COMMANDS}
    assert "/review" in names


def test_review_command_is_direct_group():
    from silica.command_table import COMMANDS
    cmd = next(c for c in COMMANDS if c.name == "/review")
    assert cmd.group == "direct"


def test_anneal_command_is_dispatchable():
    """/review only looks; /anneal is the one that acts. Advertised without a
    dispatch row it would answer "not available" in the GUI and fall through to
    the agent in the REPL."""
    from silica.command_table import COMMANDS

    cmd = next(c for c in COMMANDS if c.name == "/anneal")
    assert cmd.group == "direct" and not cmd.repl_only and cmd.handler


# ---------------------------------------------------------------------------
# C2 — per-vault keying (spec-nlp-deepening §C2.1)
# ---------------------------------------------------------------------------

def test_deferred_store_keyed_per_vault(tmp_path, monkeypatch):
    """Two vaults → two independent queues; switching back finds the bundle again."""
    from silica.kernel.write import deferred as deferred_mod
    from silica.kernel.base import home
    from silica.config import CONFIG

    monkeypatch.setattr("silica.config._SILICA_HOME", tmp_path / "silica_home")
    # Undo the conftest isolation stub: this test exercises real vault keying.
    monkeypatch.setattr(
        deferred_mod, "_store_dir", lambda: home.index_dir() / "deferred"
    )
    monkeypatch.setattr(deferred_mod, "_legacy_deferred_dir", lambda: tmp_path / "no_legacy")
    deferred_mod._stores.clear()

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "vault_a"))
    deferred_mod.get_deferred_store().put(
        "h1", "inbox/a.md", "Dir", None, [{"op": "write", "heading": "A"}]
    )

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "vault_b"))
    assert deferred_mod.get_deferred_store().get("h1") is None

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "vault_a"))
    assert deferred_mod.get_deferred_store().get("h1") is not None


def test_legacy_global_store_adopted_once(tmp_path, monkeypatch):
    """First load drains ~/.silica/deferred: real bundles adopted into the active
    vault's queue, test-fixture pollution («lint failed: ['e']») flushed."""
    import orjson
    from silica.kernel.write import deferred as deferred_mod

    legacy = tmp_path / "legacy_global"
    legacy.mkdir()
    (legacy / "realhash.json").write_bytes(orjson.dumps({
        "content_hash": "realhash", "source_path": "inbox/lez.md",
        "target_dir": "Dir", "hub": None, "timestamp": 1.0,
        "rejected_ops": [{"op": "write", "heading": "GPU", "path": "Dir/GPU.md"}],
        "rejection_reasons": {"Dir/GPU.md": "too generic"},
    }))
    (legacy / "junkhash.json").write_bytes(orjson.dumps({
        "content_hash": "junkhash", "source_path": "inbox/x.md",
        "target_dir": "Dir", "hub": None, "timestamp": 1.0,
        "rejected_ops": [{"op": "patch", "heading": "Bad", "path": "Bad.md"}],
        "rejection_reasons": {"Bad.md": "lint failed: ['e']"},
    }))
    monkeypatch.setattr(deferred_mod, "_legacy_deferred_dir", lambda: legacy)
    deferred_mod._stores.clear()

    store = deferred_mod.get_deferred_store()
    assert store.get("realhash") is not None, "real bundle must be adopted"
    assert store.get("junkhash") is None, "fixture pollution must be flushed"
    assert list(legacy.glob("*.json")) == [], "migration is one-shot: source drained"


def test_a_sweep_that_raises_is_a_line_and_the_session_goes_on(monkeypatch):
    """/anneal calls the sweep under its tool, and keeps the tool's tolerance: a
    failure prints as the error its run gave, where a raise would end the REPL."""
    from silica.commands import dispatch
    from silica.router import anneal

    def boom(**_kw):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(anneal, "sweep_deferred", boom)
    out = dispatch("/anneal --limit=2", [])
    said = [str(o) for objects, _ in out.lines for o in objects]
    assert said == ["  Annealing 2 bundle(s)…", "  [yellow]RuntimeError: store unreadable[/]"]
