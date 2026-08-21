# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Guard tests for the kernel/capabilities/tools seams a review found unguarded.

Each test pins one defect that was silent in production: an enrich that forbade
the note it was enriching, an index refresher that evicted a note whose read
merely blipped, a hand-written `write_dir: ./` that crashed vault activation,
and a model-supplied run_id that escaped ~/.silica/runs.
"""
from __future__ import annotations

import logging

import pytest


# ---------------------------------------------------------------------------
# enrich: the note must never forbid itself
# ---------------------------------------------------------------------------

def _capture_hub(monkeypatch) -> dict:
    """Run run_enrich with the rewrite skeleton stubbed; return the captured hub."""
    captured: dict = {}

    def fake_run_note_rewrite(item, config, **kwargs):
        captured.update(kwargs)
        return {"status": "no_ops"}

    monkeypatch.setattr(
        "silica.capabilities.enrich.run_note_rewrite", fake_run_note_rewrite
    )
    return captured


def test_enrich_with_empty_context_does_not_forbid_its_own_target(monkeypatch):
    """The regression: hub defaulted to the target's own title, and
    refiner_bounds turns the hub into a forbidden path matched by BASENAME —
    so every /enrich rejected its own overwrite and wrote nothing."""
    from silica.agent.bounds import refiner_bounds
    from silica.capabilities.enrich import run_enrich
    from silica.kernel.workqueue import WorkItem

    captured = _capture_hub(monkeypatch)
    run_enrich(WorkItem(kind="enrich", target_path="Notes/Target.md", context={}), None)

    assert captured["hub"] is None
    bounds = refiner_bounds("Notes/Target.md", hub=captured["hub"])
    assert bounds.allows_path("Notes/Target.md") is True

    # The shape of the old defect, so the mechanism stays documented.
    self_hubbed = refiner_bounds("Notes/Target.md", hub="Target")
    assert self_hubbed.allows_path("Notes/Target.md") is False


def test_enrich_forwards_a_declared_hub(monkeypatch):
    from silica.capabilities.enrich import run_enrich
    from silica.kernel.workqueue import WorkItem

    captured = _capture_hub(monkeypatch)
    run_enrich(
        WorkItem(kind="enrich", target_path="Notes/Target.md", context={"hub": "Concepts"}),
        None,
    )
    assert captured["hub"] == "Concepts"


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubProvider:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def call_llm(self, *, messages, **kwargs):
        self._sink.append(messages)
        return _StubResponse('{"content": "enriched"}')


def _enrich_prompt(monkeypatch, hub) -> str:
    sink: list = []
    monkeypatch.setattr(
        "silica.agent.providers.get_provider", lambda *a, **k: _StubProvider(sink)
    )
    monkeypatch.setattr(
        "silica.kernel.context_builder.build_context",
        lambda *, checkpoint_id, payload, **k: payload,
    )
    from silica.capabilities.enrich import _enrich_note

    _enrich_note(None, "Notes/Target.md", "body", hub)
    return sink[0][0]["content"]


def test_enrich_prompt_omits_the_hub_rule_when_there_is_no_hub(monkeypatch):
    """Instructing a note to wikilink to itself is meaningless, so the rule
    must disappear — and the remaining rules must stay contiguously numbered."""
    prompt = _enrich_prompt(monkeypatch, None)
    assert "You must include a wikilink" not in prompt
    assert "4. Return the result structured in JSON" in prompt
    assert "5. Return the result structured in JSON" not in prompt


def test_enrich_prompt_keeps_the_hub_rule_when_a_hub_is_declared(monkeypatch):
    prompt = _enrich_prompt(monkeypatch, "Concepts")
    assert "4. You must include a wikilink [[Concepts]]" in prompt
    assert "5. Return the result structured in JSON" in prompt


# ---------------------------------------------------------------------------
# index refreshers: a transient read failure must never prune
# ---------------------------------------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    (vault_dir / "Concepts").mkdir(parents=True)
    (vault_dir / "Concepts" / "Neural.md").write_text(
        "# Neural\n\nneural network architecture\n", encoding="utf-8"
    )
    (vault_dir / "Concepts" / "Boats.md").write_text(
        "# Boats\n\nsailing boat harbour\n", encoding="utf-8"
    )
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    import silica.kernel.recall.lexical as lex_mod
    monkeypatch.setattr(lex_mod, "_index_path", lambda: tmp_path / "lexical_index.json")
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)


def _break_read(monkeypatch, needle: str) -> None:
    """Make DRIVER.read_note fail for one note, the way a non-UTF-8 byte,
    a permission blip or an Obsidian mid-write does."""
    from silica.driver import get_driver

    drv = get_driver()
    real = drv.read_note

    def flaky(path, *a, **kw):
        if needle in path:
            raise OSError("transient read failure")
        return real(path, *a, **kw)

    monkeypatch.setattr(drv, "read_note", flaky)


def test_lexical_refresh_keeps_a_note_whose_read_failed(vault, monkeypatch):
    from silica.kernel.recall.lexical import get_lexical_store
    from silica.tools.graph import silica_lexical_refresh

    silica_lexical_refresh(force=True)
    assert any(p.endswith("Boats") for p in get_lexical_store().paths())

    _break_read(monkeypatch, "Boats")
    res = silica_lexical_refresh()

    assert any(p.endswith("Boats") for p in get_lexical_store().paths())
    assert any("Boats" in e for e in res["read_errors"])


def test_lexical_refresh_still_prunes_a_deleted_note(vault):
    """The guard must not disable GC: a note actually gone still leaves.

    The driver's listing is the authority on existence, so the delete lands as
    soon as the index sees it — reset_driver() stands in for that reindex."""
    from silica.driver import reset_driver
    from silica.kernel.recall.lexical import get_lexical_store
    from silica.tools.graph import silica_lexical_refresh

    silica_lexical_refresh(force=True)
    (vault / "Concepts" / "Boats.md").unlink()
    reset_driver()
    silica_lexical_refresh()
    assert not any(p.endswith("Boats") for p in get_lexical_store().paths())


def _spy_embed_build_index(monkeypatch) -> dict:
    seen: dict = {}

    class _Store:
        _path = "/tmp/idx.json"

        def __len__(self):
            return 0

    def fake_build_index(embedder, notes, **kwargs):
        seen.update(kwargs)
        seen["notes"] = list(notes)
        return _Store()

    monkeypatch.setattr(
        "silica.kernel.recall.embed.build_index", fake_build_index
    )
    monkeypatch.setattr(
        "silica.agent.providers.get_embedder", lambda *a, **k: object()
    )
    return seen


def test_embed_refresh_disables_prune_when_a_read_failed(vault, monkeypatch):
    from silica.tools.graph import silica_embed_refresh

    seen = _spy_embed_build_index(monkeypatch)
    _break_read(monkeypatch, "Boats")
    res = silica_embed_refresh()

    assert seen["prune"] is False
    assert any("Boats" in e for e in res["read_errors"])


def test_embed_refresh_prunes_on_a_clean_pass(vault, monkeypatch):
    from silica.tools.graph import silica_embed_refresh

    seen = _spy_embed_build_index(monkeypatch)
    res = silica_embed_refresh()

    assert seen["prune"] is True
    assert res["read_errors"] == []


# ---------------------------------------------------------------------------
# vault_manifest: `./` is the vault root, not an IndexError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", ".", "./", "././", "  ./  ", "./."])
def test_safe_rel_dir_root_forms(raw):
    from silica.kernel.vault_manifest import _safe_rel_dir

    assert _safe_rel_dir(raw) == ""


@pytest.mark.parametrize("raw", ["../out", "/abs/path", "./..", "a/../../b"])
def test_safe_rel_dir_still_rejects_escapes(raw):
    from silica.kernel.vault_manifest import _safe_rel_dir

    assert _safe_rel_dir(raw) is None


def test_load_manifest_survives_a_root_write_dir(tmp_path):
    from silica.kernel.vault_manifest import load_manifest

    (tmp_path / "vault.yaml").write_text(
        "write_dir: ./\nconventions:\n  wiki_dir: ././\n  templates_dir: ./\n",
        encoding="utf-8",
    )
    m = load_manifest(tmp_path)
    assert m.write_dir == ""
    assert m.conventions.wiki_dir == ""
    assert m.conventions.templates_dir == "templates"


# ---------------------------------------------------------------------------
# progress: a model-supplied run_id may not leave ~/.silica/runs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("run_id", ["../escape", "..", "/etc", "", "a/../../b"])
def test_run_dir_for_rejects_an_escaping_run_id(run_id):
    from silica.kernel.progress import run_dir_for

    with pytest.raises(ValueError):
        run_dir_for(run_id)


def test_run_dir_for_accepts_an_ordinary_run_id():
    from silica.kernel.progress import _RUNS_DIR, run_dir_for

    assert run_dir_for("0123456789abcdef") == _RUNS_DIR / "0123456789abcdef"


def test_progress_ledger_load_rejects_an_escaping_run_id():
    from silica.kernel.progress import ProgressLedger, RunManifest, TaskLedger

    for loader in (ProgressLedger.load, TaskLedger.load, RunManifest.load):
        with pytest.raises(ValueError):
            loader("../escape")


def test_ledger_tools_report_an_escaping_run_id_instead_of_reading_it():
    from silica.tools.atomic import silica_ledger_next, silica_ledger_update

    assert "error" in silica_ledger_next("../escape")
    assert "error" in silica_ledger_update("../escape", "t1", "done")


def test_progress_save_is_atomic(tmp_path, monkeypatch):
    """ledger.json is rewritten on every task transition; a torn write there
    makes Run.resume silently fall back to a fresh run."""
    import silica.kernel.progress as progress_mod

    monkeypatch.setattr(progress_mod, "_RUNS_DIR", tmp_path / "runs")
    calls: list = []

    from silica.kernel.recall import paths as paths_mod

    real = paths_mod.atomic_write_bytes

    def spy(path, data):
        calls.append(path)
        real(path, data)

    monkeypatch.setattr(paths_mod, "atomic_write_bytes", spy)

    ledger = progress_mod.ProgressLedger.new(mode="test")
    written = ledger.save()

    assert calls == [written]
    assert progress_mod.ProgressLedger.load(ledger.run_id).mode == "test"


# ---------------------------------------------------------------------------
# graph_report: the digest's `unresolved` total, and a traced triage failure
# ---------------------------------------------------------------------------

def _one_dangling_graph():
    nodes = [
        {"id": "a.md", "label": "a", "group": 0},
        {"id": "b.md", "label": "b", "group": 0},
        {"id": "__unresolved__Ghost", "label": "Ghost", "group": -1, "type": "ghost"},
    ]
    edges = [
        {"from": "a.md", "to": "__unresolved__Ghost", "type": "AMBIGUOUS"},
        {"from": "b.md", "to": "__unresolved__Ghost", "type": "AMBIGUOUS"},
    ]
    return nodes, edges


def test_totals_carry_unresolved_reference_count():
    """The digest header reads totals['unresolved']; compute never wrote it, so
    every vault with broken wikilinks reported `unresolved=0`."""
    from silica.kernel.report.graph_report.compute import compute_report
    from silica.kernel.report.graph_report.render import to_digest

    nodes, edges = _one_dangling_graph()
    report = compute_report(_nodes_edges_override=(nodes, edges))

    assert report.totals["unresolved"] == 2       # references
    assert report.totals["dangling_links"] == 1   # distinct missing targets
    assert "unresolved=2" in to_digest(report)


def test_triage_failure_is_logged_not_swallowed(monkeypatch, caplog):
    from silica.kernel.report.graph_report import compute as compute_mod

    class _Note:
        content = "# a\n\nbody text long enough to not be lean at all, really.\n"

    class _Driver:
        def read_note(self, nid):
            return _Note()

    monkeypatch.setattr("silica.driver.DRIVER", _Driver(), raising=False)

    def boom(_content):
        raise RuntimeError("tier probe exploded")

    monkeypatch.setattr(
        "silica.kernel.write.contested.reliability_tier", boom
    )

    nodes = [{"id": "a.md", "label": "a", "group": 0}]
    with caplog.at_level(logging.DEBUG, logger=compute_mod.__name__):
        compute_mod.compute_report(
            analytics=True, _nodes_edges_override=(nodes, []), _mtimes_override={},
        )

    assert any("a.md" in r.message and "tier probe exploded" in r.message
               for r in caplog.records)
