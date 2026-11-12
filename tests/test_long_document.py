"""A long document written from the vault (ADR-0158 points 2, 3 and 11): the
readers keep only verbatim passages, the plan keeps only passages that exist,
the writer's citations become footnotes that quote them, the register carries
what was stated to the next section, the note is stamped as written from the
vault, the caps hold where the spend happens, the local model is refused, and
a replan reads only what is new or changed."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from silica.sources import long_document as ld

BRIDGE = """---
tags: [engineering]
---
An overview of the case.

## Collapse

The bridge fell on 14 August 2018 at 11:36.
Forty-three people died in the collapse.
"""


def _model(calls: list, *, read=None, plan=None, write=None):
    """The model stood in for, by stage: each stage's reply comes from the
    function given for it, called with the user message."""
    def ask(messages):
        system, user = messages[0]["content"], messages[1]["content"]
        stage = {ld._READ_SYSTEM: "read", ld._PLAN_SYSTEM: "plan", ld._WRITE_SYSTEM: "write",
                 ld._FORMAT_SYSTEM: "format"}[system]
        calls.append((stage, user))
        reply = {"read": read, "plan": plan, "write": write}.get(stage)
        return reply(user) if reply else json.dumps({"passages": []})

    return ask


def _reads(*quotes: tuple[str, str]):
    return lambda user: json.dumps({"passages": [{"quote": q, "why": w} for q, w in quotes]})


def _plan_reply(*sections: tuple[str, int, list[str]], gaps=(), title="The fall of the bridge"):
    return lambda user: json.dumps({"title": title, "gaps": list(gaps), "sections": [
        {"title": t, "brief": f"About {t}.", "words": w, "passages": ids} for t, w, ids in sections]})


def _started(type_id="investigation", selection=None, params=None):
    """start() with its thread doing nothing, so the test runs the stages itself."""
    got: dict = {}
    ready = threading.Event()
    ld.start(type_id, selection or {"folders": ["Notes"]}, params or {"focus": "why the bridge fell"},
             run=lambda rec, stop: (got.update(rec=rec, stop=stop), ready.set()))
    assert ready.wait(5)
    return got["rec"], got["stop"]


def _planned(monkeypatch, calls, *, plan, read=None, type_id="investigation"):
    monkeypatch.setattr(ld, "_ask", _model(calls, read=read or _reads(
        ("The bridge fell on 14 August 2018 at 11:36.", "the date"),
        ("Forty-three people died in the collapse.", "the toll")), plan=plan))
    rec, stop = _started(type_id)
    ld._run_plan(rec, stop)
    return ld.read(rec["id"])


def test_read_keeps_only_verbatim_passages_with_an_anchor_code_computed(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    calls: list = []
    rec = _planned(monkeypatch, calls, plan=_plan_reply(("What fell", 800, ["P1", "P2"])), read=_reads(
        ("The bridge fell on 14 August 2018 at 11:36.", "the date"),
        ("The bridge fell in the summer of 2018.", "a paraphrase"),
        ("Forty-three   people\ndied in the  collapse.", "the toll, spaced otherwise"),
        ("bridge fell", "too short to stand on"),
    ))

    passages = ld._side(rec["id"])["passages"]
    assert [p["quote"] for p in passages.values()] == [
        "The bridge fell on 14 August 2018 at 11:36.", "Forty-three   people\ndied in the  collapse."]
    assert list(passages) == ["P1", "P2"]
    assert {p["anchor"] for p in passages.values()} == {"§ Collapse"}
    # the reader saw the type and the person's parameters verbatim, and the
    # digests stay out of the record every listing parses
    read_user = next(u for s, u in calls if s == "read")
    assert "Investigation" in read_user and '"focus": "why the bridge fell"' in read_user
    assert "quote" not in json.dumps(rec)
    assert rec["state"] == "planned" and rec["stage"] == ""


def test_a_pdf_page_is_the_anchor_of_a_passage_found_on_it():
    text = "<!-- p. 1 -->\nCover.\n\n<!-- p. 2 -->\n## Methods\nWe sampled forty rivers.\n"
    assert ld._anchor(text, "We sampled forty rivers.") == "p. 2"     # a text layer's # is no heading
    assert ld._anchor("Top.\n\n## Methods\n\nWe sampled forty rivers.\n", "sampled  forty rivers") == "§ Methods"
    long = "a b c\n\n" * (ld.PART_CHARS // 6)
    assert all(len(p) <= ld.PART_CHARS for p in ld._parts(long)) and "".join(ld._parts(long)) == long


def test_plan_drops_unknown_ids_and_marks_thin_sections_and_gaps(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    rec = _planned(monkeypatch, [], plan=_plan_reply(
        ("What fell", 600, ["P1", "P2", "P99"]), ("Why", 400, ["P2"]), ("After", 200, ["P42"]),
        gaps=["the inquiry's findings"]))

    sections = rec["outline"]["sections"]
    assert [s["passages"] for s in sections] == [["P1", "P2"], ["P2"], []]
    assert [s["thin"] for s in sections] == [False, True, True]
    assert sections[0]["notes"] == ["Notes/Bridge.md"] and sections[2]["notes"] == []
    assert rec["outline"]["gaps"] == ["the inquiry's findings"]
    assert rec["outline"]["title"] == "The fall of the bridge" == rec["source"]["name"]


def test_nothing_that_bears_on_the_document_fails_with_a_sentence(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    rec = _planned(monkeypatch, [], plan=_plan_reply(("x", 100, [])), read=_reads())
    assert rec["state"] == "failed" and "no passage" in rec["error"]


def _write(rec: dict, sections: list[dict], **extra) -> dict:
    got: dict = {}
    ready = threading.Event()
    ld.write(rec["id"], {"sections": sections, **extra},
             run=lambda r, stop: (got.update(rec=r, stop=stop), ready.set()))
    assert ready.wait(5)
    ld._run_write(got["rec"], got["stop"])
    return ld.read(rec["id"])


def test_write_turns_cites_into_footnotes_drops_phantoms_and_carries_the_register(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    calls: list = []
    rec = _planned(monkeypatch, calls, plan=_plan_reply(("What fell", 600, ["P1", "P2"]), ("After", 400, ["P2"])))
    replies = iter([
        "## What fell\n\nThe bridge fell on 14 August 2018 [P1]. Forty-three died [P2, P99].\n\n"
        "REGISTER:\n- 14 August 2018\n- forty-three dead",
        "The toll stood [P2]. It fell at noon [P1].\n\nREGISTER:\n- noon",
    ])
    monkeypatch.setattr(ld, "_ask", _model(calls, write=lambda user: next(replies)))

    done = _write(rec, rec["outline"]["sections"])

    assert done["state"] == "done" and done["note"].startswith("Documents/")
    assert done["source"]["path"] == done["note"]
    second = [u for s, u in calls if s == "write"][1]
    assert "- 14 August 2018" in second and "- forty-three dead" in second   # the register reached it
    assert "Forty-three died [P2, P99]." in second                            # and the last paragraph
    text = (Path(tmp_vault.note("x.md")).parent / done["note"]).read_text()
    assert "fell on 14 August 2018 [^P1]. Forty-three died [^P2]." in text
    # P1 exists but the second section was never shown it: dropped like a phantom
    assert "It fell at noon." in text and "P99" not in text and "REGISTER" not in text
    assert text.count("## What fell") == 1 and "## After" in text
    assert '[^P1]: [[Bridge]], § Collapse: "The bridge fell on 14 August 2018 at 11:36."' in text
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["written_from"] == "vault" and front["document_type"] == "investigation"
    assert front["sources"] == ["[[Bridge]]"] and front["title"] == "The fall of the bridge"


def test_a_short_type_writes_once(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    calls: list = []
    rec = _planned(monkeypatch, calls, type_id="decision_record",
                   plan=_plan_reply(("Context", 300, ["P1"]), ("Decision", 300, ["P2"])))
    monkeypatch.setattr(ld, "_ask", _model(calls, write=lambda u: "## Context\n\nIt fell [P1].\n\n"
                                                                  "## Decision\n\nRebuild [P2]."))
    done = _write(rec, rec["outline"]["sections"])
    assert [s for s, _ in calls].count("write") == 1 and done["state"] == "done"


def test_a_stopped_write_keeps_the_outline(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    rec = _planned(monkeypatch, [], plan=_plan_reply(("What fell", 600, ["P1", "P2"])))
    got: dict = {}
    ready = threading.Event()
    ld.write(rec["id"], {"sections": rec["outline"]["sections"]},
             run=lambda r, stop: (got.update(rec=r, stop=stop), ready.set()))
    assert ready.wait(5)
    got["stop"].set()
    ld._run_write(got["rec"], got["stop"])
    after = ld.read(rec["id"])
    assert after["state"] == "planned" and after["error"] == ld.STOPPED and after["outline"]["sections"]


def test_caps_are_refused_where_the_spend_happens(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    rec = _planned(monkeypatch, [], plan=_plan_reply(("What fell", 600, ["P1", "P2"])))
    one = {"title": "S", "brief": "", "words": 500, "passages": ["P1"]}
    with pytest.raises(ld.Refused, match="at most 30 sections"):
        ld.write(rec["id"], {"sections": [one] * 31})
    with pytest.raises(ld.Refused, match="8,000 words"):
        ld.write(rec["id"], {"sections": [{**one, "words": 8001}]})
    with pytest.raises(ld.Refused, match="16,000 words in all"):            # investigation: 8,000 x 2
        ld.write(rec["id"], {"sections": [{**one, "words": 6000}] * 3})
    with pytest.raises(ld.Refused, match="keep at least one"):
        ld.write(rec["id"], {"sections": [{**one, "passages": ["P99"]}]})
    with pytest.raises(ld.Refused, match="100 to 16,000 words"):
        ld.start("investigation", {"folders": ["Notes"]}, {"length": 16001})
    with pytest.raises(ld.Refused):
        ld.start("limerick", {"folders": ["Notes"]}, {})
    assert ld.read(rec["id"])["state"] == "planned"


def test_one_document_at_a_time(tmp_vault):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    _started()
    with pytest.raises(ld.Refused, match="already being read or written"):
        ld.start("speech", {"folders": ["Notes"]}, {})


def test_the_selection_skips_silicas_own_folders_and_its_own_documents(tmp_vault):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    tmp_vault.note("Inbox/Draft.md", "Waiting to be nucleated.\n")
    tmp_vault.note("sources/Bridge.md", "The leaf.\n")
    tmp_vault.note("Documents/Old.md", "---\nwritten_from: vault\n---\nA book made of the notes.\n")
    tmp_vault.note("Workspaces/Study.md", '---\ntype: workspace\nnotes:\n  - "[[Bridge]]"\n---\n')

    sel = ld._clean_selection({"folders": ["Notes", "Inbox", "sources", "Documents"]})
    assert ld.resolve(sel) == ["Notes/Bridge.md"]
    assert ld.resolve(ld._clean_selection({"workspaces": ["Workspaces/Study.md"]})) == ["Notes/Bridge.md"]
    assert ld.count({"folders": ["Notes"]}) == {"notes": 1, "pdfs": 0, "pages": 0, "skipped": 0,
                                                "words": len(BRIDGE.split("---\n", 2)[2].split())}
    for bad in ({"folders": ["../outside"]}, {"folders": [".obsidian"]}, {}, {"workspaces": ["Nowhere.md"]}):
        with pytest.raises(ld.Refused):
            ld.resolve(ld._clean_selection(bad))


def test_replan_reads_only_new_or_changed_notes(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/A.md", "Alpha holds the first fact of all.\n")
    b = tmp_vault.note("Notes/B.md", "Beta holds the second fact of all.\n")
    calls: list = []

    def read(user):
        line = user.rsplit("\n", 2)[-2]
        return json.dumps({"passages": [{"quote": line, "why": "a fact"}]})

    rec = _planned(monkeypatch, calls, read=read, plan=_plan_reply(("All", 500, ["P1", "P2"])))
    assert sorted(p["note"] for p in ld._side(rec["id"])["passages"].values()) == ["Notes/A.md", "Notes/B.md"]
    tmp_vault.write(b, "Beta now holds a changed fact of all.\n")
    os.utime(b, (os.stat(b).st_atime, os.stat(b).st_mtime + 5))
    from silica.driver import DRIVER

    # through the driver, as /nucleate writes it, so the index sees it at once
    DRIVER.create("Notes/C.md", "Gamma arrived from a deep research.\n")
    calls.clear()

    got: dict = {}
    ready = threading.Event()
    ld.replan(rec["id"], run=lambda r, stop: (got.update(rec=r, stop=stop), ready.set()))
    assert ready.wait(5)
    ld._run_plan(got["rec"], got["stop"])

    read_users = [u for s, u in calls if s == "read"]
    assert len(read_users) == 2 and not any("Alpha" in u for u in read_users)
    side = ld._side(rec["id"])
    # A keeps its id, B's old one is gone and never handed out again
    assert {pid: p["note"] for pid, p in side["passages"].items()} == {
        "P1": "Notes/A.md", "P3": "Notes/B.md", "P4": "Notes/C.md"}
    assert ld.read(rec["id"])["state"] == "planned"


@pytest.fixture
def client(tmp_vault):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()
    return TestClient(server.app)


def test_the_local_model_is_refused_and_the_form_gets_its_types(client, tmp_vault, monkeypatch):
    from silica.onboarding import settings_ops

    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    monkeypatch.setattr(settings_ops, "base_urls", lambda: {"chat": "http://localhost:1234/v1"})
    r = client.post("/document/start", json={"type": "speech", "selection": {"folders": ["Notes"]}, "params": {}})
    assert r.status_code == 409 and "hosted model" in r.json()["error"]
    listed = client.get("/document").json()
    assert listed["documents"] == []
    assert listed["types"]["speech"] == {"label": "Speech", "short": True, "words": 1500}
    assert listed["types"]["book"]["short"] is False
    assert client.post("/document/count", json={"selection": {"folders": ["Notes"]}}).json()["notes"] == 1
    r = client.post("/document/count", json={"selection": {"folders": ["../x"]}})
    assert r.status_code == 409 and r.json()["error"] == "a folder in the selection is not one of this vault's"


def test_a_pdf_is_read_page_by_page_unless_a_chosen_note_holds_its_text(tmp_vault, monkeypatch):
    from silica.config import CONFIG
    from silica.sources import pdf_extract
    from tests.doc_factory import pdf_bytes

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    pdf_extract._CACHE.clear()
    root = Path(tmp_vault.note("Notes/Bridge.md", BRIDGE)).parent
    (root / "Survey.pdf").write_bytes(pdf_bytes(["The cover of the survey.", "We sampled forty rivers in 2019."]))
    (root / "Held.pdf").write_bytes(pdf_bytes(["Text a note already holds."]))
    tmp_vault.note("Notes/Held notes.md", "---\nsource_file: Notes/Held.pdf\n---\nText a note already holds.\n")

    assert ld.resolve(ld._clean_selection({"folders": ["Notes"]})) == [
        "Notes/Bridge.md", "Notes/Held notes.md", "Notes/Survey.pdf"]
    counted = ld.count({"folders": ["Notes"]})
    assert counted["pdfs"] == 1 and counted["pages"] == 2 and counted["notes"] == 2
    rec = _planned(monkeypatch, [], plan=_plan_reply(("Rivers", 300, ["P1"])), read=lambda user: json.dumps(
        {"passages": [{"quote": "We sampled forty rivers in 2019.", "why": "the sample"}]}
        if "Survey.pdf" in user else {"passages": []}))
    (passage,) = ld._side(rec["id"])["passages"].values()
    assert passage == {"note": "Notes/Survey.pdf", "anchor": "p. 2", "quote": "We sampled forty rivers in 2019.",
                       "why": "the sample"}


def test_a_screenplay_is_told_as_story_then_laid_out(tmp_vault, monkeypatch):
    tmp_vault.note("Notes/Bridge.md", BRIDGE)
    calls: list = []
    rec = _planned(monkeypatch, calls, type_id="screenplay", plan=_plan_reply(("The fall", 900, ["P1", "P2"])))
    monkeypatch.setattr(ld, "_ask", _model(calls, write=lambda u: "The bridge falls [P1].\n\nREGISTER:\n- 11:36"))
    script = "EXT. VIADUCT - DAY\n\nThe deck gives way. [P1]"
    original = ld._ask
    monkeypatch.setattr(ld, "_ask", lambda m: script if m[0]["content"] is ld._FORMAT_SYSTEM else original(m))
    done = _write(rec, rec["outline"]["sections"])
    text = (Path(ld.CONFIG.vault_path) / done["note"]).read_text()
    assert "EXT. VIADUCT - DAY" in text and "gives way. [^P1]" in text and "REGISTER" not in text


def test_a_usage_limit_mid_read_keeps_the_passages_already_paid_for(tmp_vault, monkeypatch):
    from silica.model.usage_limits import UsageLimited

    tmp_vault.note("Notes/A.md", "Alpha holds the first fact of all.\n")
    tmp_vault.note("Notes/B.md", "Beta holds the second fact of all.\n")
    monkeypatch.setattr(ld.CONFIG, "subagent_max_concurrent", 1)

    def read(user):
        if "Beta" in user:
            raise UsageLimited("window", None)
        return json.dumps({"passages": [{"quote": "Alpha holds the first fact of all.", "why": "a fact"}]})

    rec = _planned(monkeypatch, [], read=read, plan=_plan_reply(("All", 500, ["P1"])))
    assert rec["state"] == "failed" and rec["error"] == "the 5-hour usage limit was reached"
    assert [p["note"] for p in ld._side(rec["id"])["passages"].values()] == ["Notes/A.md"]


def test_the_register_trailer_is_read_however_it_is_headed():
    for line in ("REGISTER:", "## Register", "**REGISTER:** none"):
        prose, items = ld._split_register(f"It fell. [P1]\nRegister offices shut.\n\n{line}\n- 11:36\n")
        assert prose == "It fell. [P1]\nRegister offices shut." and items == ["11:36"]


def test_a_cut_index_keeps_every_notes_first_passages(monkeypatch):
    passages = {f"P{i}": {"note": "A.md" if i <= 5 else "B.md", "why": "w"} for i in range(1, 8)}
    monkeypatch.setattr(ld, "PLAN_INDEX_CHARS", 60)
    index, left_out = ld._index(passages)
    assert [line.split(" | ")[0] for line in index.splitlines()] == ["P1", "P6", "P2", "P7"] and left_out == 3
