"""Ingest as a service: one way into the Coordinator, one triage, for every surface."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources import ingest

ROOT = Path(__file__).resolve().parents[1]


def test_only_distill_constructs_a_coordinator():
    """Two ways in drifted on the conversion, the apparatus filter and keep_sources."""
    sites = []
    for path in (ROOT / "silica").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and "Coordinator" in (getattr(node.func, "id", ""), getattr(node.func, "attr", "")):
                sites.append(path.relative_to(ROOT).as_posix())
    assert sites == ["silica/sources/ingest.py"]


def test_keep_sources_cannot_be_left_to_the_coordinators_default():
    with pytest.raises(TypeError, match="keep_sources"):
        ingest.distill(["Inbox/a.md"], "T")


@pytest.fixture
def runs(monkeypatch):
    calls: list[dict] = []

    class _Coordinator:
        def __init__(self, **kw):
            calls.append(kw)

        def run(self):
            return {"final_status": "Success"}

    import silica.router.coordinator as coord_mod

    monkeypatch.setattr(coord_mod, "Coordinator", _Coordinator)
    return calls


def test_the_tool_drops_apparatus_and_keeps_sources_as_a_typed_nucleate_does(tmp_vault, runs):
    from silica.tools.runners import silica_run_injector

    tmp_vault.note("Inbox/body.md", "# A chapter\n\nProse.\n")
    tmp_vault.note("Inbox/refs.md", "---\nreferences: true\n---\n\n# References\n")
    out = silica_run_injector(inbox_files=["Inbox/body.md", "Inbox/refs.md"], target_dir="T")
    assert [c["inbox_files"] for c in runs] == [["Inbox/body.md"]]
    assert runs[0]["keep_sources"] is True
    assert (out["files_total"], out["apparatus_skipped"]) == (1, 1)


def test_triage_says_converter_or_what_the_source_did(tmp_vault):
    tmp_vault.note("Inbox/a.md", "# A\n")
    assert ingest.triage("Inbox/talk.mp3", None) is None
    assert ingest.triage("Inbox/a.md", None)["status"] == "distill"


def test_owed_leaves_a_glob_or_a_folder_to_the_command(tmp_vault):
    (Path(CONFIG.vault_path) / "Inbox" / "dir").mkdir(parents=True)
    assert ingest.owed(["Inbox/*.pdf"]) == []
    assert ingest.owed(["Inbox/dir"]) == []


def test_a_real_tool_call_announces_nothing_of_its_own(tmp_vault, runs):
    """The agent loop draws a tool's block. Only a typed /nucleate, which no loop
    runs, says so on `work/tool`: said here too, the web would draw it twice."""
    from silica.obs.bus import BUS
    from silica.tools.runners import silica_run_injector

    said: list = []
    BUS.subscribe("work/tool", said.append)
    try:
        tmp_vault.note("Inbox/body.md", "# A chapter\n\nProse.\n")
        silica_run_injector(inbox_files=["Inbox/body.md"], target_dir="T")
    finally:
        BUS.unsubscribe("work/tool", said.append)
    assert runs and said == []
