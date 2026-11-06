# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a tool is, declared where it is, and proved for the whole registry.

The sets these facts replaced were name lists nothing joined, so a new tool that
brought outside text and missed one passed the taint gate without a word
(ADR-0090). Every test here runs over every registered tool: a tool added
tomorrow is covered by being registered.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from silica.agent import memory_gate
from silica.tools import TOOLS, Reads, catalog, note_keys

SILICA_ROOT = Path(__file__).resolve().parent.parent / "silica"

# The sets as they stood before they were derived (2026-11-06), each one a
# behaviour: a fact that moves changes what a turn offers, compacts or taints.
INGRESS = {"silica_web_answer", "silica_run_injector", "silica_read", "web_fetch", "web_search",
           "find_in_page", "remember", "silica_session_search"}
PARALLEL = {"silica_search_context", "silica_read", "silica_read_note", "silica_outline",
            "silica_files", "silica_exists", "silica_search", "silica_semantic_search",
            "silica_related", "silica_timeline", "silica_recall", "silica_concepts",
            "silica_links", "silica_backlinks", "silica_props", "silica_file_links",
            "silica_unresolved", "silica_orphans", "silica_graph_path", "silica_vaults"}
MUTATES = {"silica_write_note": "written", "silica_patch_note": "written",
           "silica_flag_note": "written", "silica_require": "written", "silica_restore": "written",
           "silica_delete": "deleted", "silica_move": "moved"}
FOUND_NOTHING = {"silica_recall": "notes", "silica_semantic_search": "results",
                 "silica_search_context": "notes_matched", "silica_search": "paths"}
READS = {"silica_read_note": Reads(args=("name",)), "silica_outline": Reads(args=("name",)),
         "silica_read": Reads(args=("path",), result="path"),
         "silica_recall": Reads(args=("notes",), result="notes"),
         "silica_search_context": Reads(result="hits")}


def _params(tool) -> set[str]:
    return set(tool.params_model.model_json_schema().get("properties", {}))


def test_every_tool_says_whether_it_brings_outside_text_in():
    """The ADR-0090 hole, closed by construction: `ingress` has no False default
    a forgotten tool could fall through."""
    undeclared = sorted(n for n, t in TOOLS.items() if t.ingress is None)
    assert not undeclared, f"declare ingress=True or False on {undeclared}"


def test_the_derived_sets_are_the_ones_they_replaced():
    assert {n for n, t in TOOLS.items() if t.ingress} == INGRESS
    assert {n for n, t in TOOLS.items() if t.parallel_safe} == PARALLEL
    assert {n: t.mutates for n, t in TOOLS.items() if t.mutates} == MUTATES
    assert {n: t.found_nothing for n, t in TOOLS.items() if t.found_nothing} == FOUND_NOTHING
    assert {n: t.reads for n, t in TOOLS.items() if t.reads} == READS
    assert {n for n, t in TOOLS.items() if t.once_per_block} == {"silica_recall"}


def test_a_fact_that_names_an_argument_names_a_real_one():
    for name, t in TOOLS.items():
        params = _params(t)
        named = [*t.note_args, *(t.reads.args if t.reads else ())]
        if t.label and t.label[1]:
            named.append(t.label[1])
        if t.reply_flag:
            named.append(t.reply_flag)
        assert set(named) <= params, f"{name} declares {sorted(set(named) - params)}"


def test_the_note_arguments_are_the_ones_both_readers_held():
    """compaction and the chat footer each kept their own list, with different
    members (H-tool-4): `note_path` is the footer's alone, and compaction says why."""
    from silica.agent import compaction

    assert set(note_keys()) == {"name", "note", "path", "note_path", "ref"}
    assert set(compaction._read_keys(TOOLS)) == {"name", "note", "path", "ref"}


def test_a_call_to_a_tool_no_longer_registered_still_names_its_note():
    """A stored chat outlives the tool: the chip and the compaction read the
    arguments, whichever tool held them."""
    from silica.ui.web.callback import _note_refs

    assert "silica_gone" not in TOOLS
    assert _note_refs({"path": "a/b.md"}) == ["a/b.md"]


def test_a_write_never_runs_beside_another_call():
    both = sorted(n for n, t in TOOLS.items() if t.mutates and t.parallel_safe)
    assert not both, f"a parallel block must hold reads only: {both}"


def test_a_tool_that_mutates_a_note_names_it():
    """`silica_restore` names none: it is internal, its inverses carry the paths,
    and no chat history holds a call to it."""
    silent = sorted(n for n, t in TOOLS.items() if t.mutates and not t.note_args)
    assert silent == ["silica_restore"]


@pytest.mark.parametrize("name", sorted(INGRESS))
def test_the_gate_reads_the_declared_fact(name):
    assert memory_gate.ingress(name)


def test_a_connector_taints_even_when_it_is_configured_no_longer():
    assert "mcp__gone" not in TOOLS
    assert memory_gate.ingress("mcp__gone")
    assert not memory_gate.ingress("silica_search")


def test_a_returned_path_nothing_can_check_refuses_memory(monkeypatch):
    """A gate that cannot look must not pass: fail closed, as an unreadable
    import record does."""
    monkeypatch.setattr(memory_gate, "mail_check", memory_gate._no_mail_check)
    turn = memory_gate.begin_turn([{"role": "user", "content": "hi"}], chat=True)
    memory_gate.returned_notes(["a.md"])
    assert memory_gate.after_tool(turn, "silica_search")


# --- the catalog -------------------------------------------------------------

def test_every_module_that_registers_a_tool_is_in_the_catalog():
    """An AST scan and not TOOLS: a module the catalog forgot is exactly the one
    TOOLS would not show."""
    decorated = set()
    for path in SILICA_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                called = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(called, ast.Name) and called.id == "tool":
                    rel = path.relative_to(SILICA_ROOT.parent).with_suffix("")
                    decorated.add(".".join(rel.parts))
    assert decorated - {"silica.tools.__init__"} <= set(catalog._MODULES)


def test_the_catalog_holds_the_prompt_order():
    """The loader's description walks TOOLS, and it is part of the prompt prefix
    (ADR-0050): module order is what fixes it, whoever imported what first."""
    modules = [TOOLS[n].fn.__module__ for n in TOOLS if not n.startswith("mcp__")]
    ranks = [catalog._MODULES.index(m) for m in modules]
    assert ranks == sorted(ranks)


def test_load_is_idempotent_and_leaves_the_order_alone():
    before = list(TOOLS)
    assert catalog.load() is TOOLS
    assert list(TOOLS) == before


def test_the_rows_waiting_for_their_decorators_are_only_those():
    """catalog._DECLARED_HERE is a shortcut with a date. Shrinking it is the
    point; a row added for a file this lane could edit is the old name list
    coming back."""
    assert len(catalog._DECLARED_HERE) <= 16
    for name in catalog._DECLARED_HERE:
        module = TOOLS[name].fn.__module__
        assert module in {"silica.tools.runners", "silica.tools.atomic",
                          "silica.sources.web_fetch", "silica.sources.web_research"}


# --- the lanes the tool bodies moved to --------------------------------------

def test_the_engine_refuses_to_run_without_a_pdf_reader(monkeypatch):
    """Sources sits above recall, so the reader is handed over and not imported.
    Unset, the engine must refuse: its own reader opens PDFs in this process."""
    from silica.kernel.recall import engine

    monkeypatch.setattr(engine, "pdf_reader", None)
    with pytest.raises(RuntimeError, match="no PDF reader"):
        engine.engine()


def test_write_note_refuses_in_the_tools_order(tmp_path, monkeypatch):
    """bind and gate are the tool's, and each runs where its refusal always
    landed: a bad path and a reserved key before the binding, the whole note
    built before the gate. A model reads these in this order."""
    from silica.config import CONFIG
    from silica.kernel.write.notes import write_note

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    seen: list[str] = []

    def bind():
        seen.append("bind")
        return [], None, "bad binding"

    def gate(path):
        seen.append("gate")
        return {"error": "bad diagram"}

    assert "Refused path" in write_note("../out.md", "b", bind=bind)["error"]
    assert "Reserved" in write_note("n.md", "b", props={"AI": "x"}, bind=bind)["error"]
    assert seen == []
    assert write_note("n.md", "b", bind=bind, gate=gate) == {"error": "bad binding"}
    assert seen == ["bind"]
    assert write_note("n.md", "b", template="no-such-template", gate=gate)["error"]
    assert seen == ["bind"]
    assert write_note("n.md", "b", gate=gate) == {"error": "bad diagram"}
    assert seen == ["bind", "gate"]


def test_write_note_needs_nothing_from_the_registry(tmp_path, monkeypatch):
    """The Learn lane's way out of TOOLS["silica_write_note"].fn."""
    from silica.config import CONFIG
    from silica.kernel.write.notes import write_note

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("silica.tools.TOOLS", {})
        out = write_note("Plans/n.md", "body", title="N")
    assert out["success"] and out["path"] == "Plans/n.md"
    assert "error" in write_note("Plans/n.md", "again")


def test_the_three_refreshes_read_the_same_raw_body(monkeypatch):
    """H-index-1: one loop, so the three cannot sign a note differently."""
    from types import SimpleNamespace

    from silica.kernel.recall import refresh

    calls: list[str] = []
    monkeypatch.setattr(refresh, "DRIVER", SimpleNamespace(
        list_files=lambda folder: [SimpleNamespace(path="a/N.md", name="N"),
                                   SimpleNamespace(path="a/Bad.md", name="Bad")],
        read_note=lambda path: (calls.append(path) or SimpleNamespace(content="---\nk: v\n---\nraw"))
        if path != "a/Bad.md" else (_ for _ in ()).throw(OSError("mid-write"))))
    refs, notes, errors, refused = refresh._read_all("")
    assert notes == [("a/N", "N", "---\nk: v\n---\nraw")]
    assert errors == ["a/Bad.md: mid-write"] and len(refs) == 2 and refused is None
