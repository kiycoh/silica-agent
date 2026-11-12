"""A deep research (ADR-0158 point 8): the plan, the job that runs one loop per
sub-question, the parent note, the per-run search state that keeps a chat
question from clearing the job's bank, citations that name their quote
(point 9), and the scholarly lane (point 10)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from silica.sources import deep_research as dr
from silica.sources import web_research as wr


def _plan_reply(*subs: tuple[str, str], protocol: str = "Peer-reviewed studies only.") -> str:
    return json.dumps({"protocol": protocol, "subquestions": [{"text": t, "held": h} for t, h in subs]})


def test_the_plan_marks_what_the_vault_holds_and_only_titles_it_was_shown(tmp_vault, monkeypatch):
    monkeypatch.setattr(dr, "_vault_near", lambda text, k=12: {"Lipid nanoparticles": "Notes/Lipid nanoparticles.md"})
    seen: list = []

    def ask(messages):
        seen.append(messages)
        return _plan_reply(("What is in an mRNA vaccine?", ""), ("What do lipid nanoparticles do?", "Lipid nanoparticles"),
                           ("Where do studies disagree?", "A title it was never shown"))

    p = dr.plan("literature", "How do mRNA vaccines differ from vector vaccines?", ask=ask)

    assert [x["held"] for x in p["parts"]] == ["", "Notes/Lipid nanoparticles.md", ""]
    assert p["protocol"] == "Peer-reviewed studies only."
    assert "Lipid nanoparticles" in seen[0][1]["content"]
    assert "literature review" in seen[0][0]["content"]


def test_the_plan_is_cut_at_the_depth_and_refuses_what_it_cannot_plan(tmp_vault, monkeypatch):
    monkeypatch.setattr(dr, "_vault_near", lambda text, k=12: {})
    many = _plan_reply(*[(f"Question {i}?", "") for i in range(10)])
    assert len(dr.plan("cluster", "Immunology", depth="quick", ask=lambda m: many)["parts"]) == 4
    with pytest.raises(dr.Refused):
        dr.plan("literature", "  ", ask=lambda m: many)
    with pytest.raises(dr.Refused):
        dr.plan("anchored", "", ask=lambda m: many)
    with pytest.raises(dr.Refused):
        dr.plan("literature", "Q", ask=lambda m: "not json at all")


def test_an_anchored_plan_reads_the_note_it_looks_into(tmp_vault, monkeypatch):
    monkeypatch.setattr(dr, "_vault_near", lambda text, k=12: {})
    tmp_vault.note("Inbox/Cell lecture.md", "---\nsource: youtube\n---\n[04:12] Mitochondria make all ATP.\n")
    seen: list = []
    p = dr.plan("anchored", "", note="Inbox/Cell lecture.md",
                ask=lambda m: seen.append(m) or _plan_reply(("[04:12] Do mitochondria make all ATP?", "")))
    assert "Mitochondria make all ATP" in seen[0][1]["content"]
    assert p["question"] == "Cell lecture"
    assert dr._part_name(p["parts"][0]["q"]) == "Do mitochondria make all ATP"


def _approved(n: int = 3, held: int | None = None) -> dict:
    parts = [{"q": f"Sub-question {i}?", "held": ""} for i in range(n)]
    if held is not None:
        parts[held]["held"] = "Notes/Held.md"
    return {"shape": "literature", "question": "How are vaccines built?", "depth": "standard",
            "protocol": "Studies.", "parts": parts}


def _started(plan: dict) -> tuple[dict, threading.Event]:
    """start() with its thread doing nothing, so the test runs the job itself."""
    got: dict = {}
    ready = threading.Event()
    dr.start(plan, run=lambda rec, stop: (got.update(rec=rec, stop=stop), ready.set()))
    assert ready.wait(5)
    return got["rec"], got["stop"]


def _fake_loop(found: dict[str, int], calls: list):
    """web_research stood in for: banks `found[q]` quotes in the caller's own
    state and writes the part, or raises the loop's "no findings"."""
    from silica.driver import DRIVER

    def run(concept, max_searches=0, tool_progress_callback=None, *, folder="", name="",
            quote_ids=False, scholarly=False, cancel_token=None):
        calls.append({"q": concept, "folder": folder, "quote_ids": quote_ids, "cap": max_searches,
                      "own": wr._OWN.get() is not None})
        wr._reset_turn()
        if not found.get(concept):
            raise ValueError("no findings")
        for i in range(found[concept]):
            wr._turn().bank[f"Q{i + 1}"] = wr.Quote(f"https://a.test/{i}", f"quote {i}", f"why {concept} {i}")
        rel = wr.unique_inbox_path(name or concept, folder=folder)
        DRIVER.create(rel, "---\ntitle: x\n---\n\nA claim. [Q1]\n")
        return rel

    return run


def test_the_job_runs_a_loop_per_sub_question_in_its_own_state_and_writes_the_parent(tmp_vault, monkeypatch):
    calls: list = []
    monkeypatch.setattr(wr, "web_research", _fake_loop({"Sub-question 0?": 5, "Sub-question 2?": 1}, calls))
    monkeypatch.setattr(dr, "_ask", lambda m: json.dumps(["Adjuvants", "Cold chain"]))
    wr._SHARED.bank["Q9"] = wr.Quote("https://chat.test", "the chat's own", "w")
    rec, stop = _started(_approved(4, held=3))

    dr._run(rec, stop)

    on_disk = dr.read(rec["id"])
    assert on_disk["state"] == "done"
    assert [p["state"] for p in on_disk["parts"]] == ["done", "empty", "done", "held"]
    assert [p["quotes"] for p in on_disk["parts"]] == [5, 0, 1, 0]
    assert on_disk["topics"] == ["Adjuvants", "Cold chain"]
    # one loop per sub-question not held, each in a state of its own, citing quotes
    assert [c["q"] for c in calls] == ["Sub-question 0?", "Sub-question 1?", "Sub-question 2?"]
    assert all(c["own"] and c["quote_ids"] and c["cap"] == dr.DEPTHS["standard"]["searches"] for c in calls)
    # the chat's bank was never touched by the job's loops
    assert list(wr._SHARED.bank) == ["Q9"]
    parent = (Path(dr.CONFIG.vault_path) / rec["parent"]).read_text()
    assert "document: true" in parent and "source: deep-research" in parent
    assert "nothing found, no part written" in parent
    assert "your vault has [[Notes/Held]]" in parent
    assert "- Adjuvants" in parent
    folder = Path(rec["parent"]).stem
    assert all(c["folder"] == folder for c in calls)
    assert on_disk["parts"][0]["note"].startswith(rec["folder"] + "/")
    wr._SHARED.clear()


def test_a_stop_skips_the_parts_not_begun(tmp_vault, monkeypatch):
    calls: list = []
    loop = _fake_loop({"Sub-question 0?": 3, "Sub-question 1?": 3}, calls)

    def stopping(concept, *a, **kw):
        out = loop(concept, *a, **kw)
        kw["cancel_token"].set()
        return out

    monkeypatch.setattr(wr, "web_research", stopping)
    rec, stop = _started(_approved(3))
    dr._run(rec, stop)
    on_disk = dr.read(rec["id"])
    assert [p["state"] for p in on_disk["parts"]] == ["done", "skipped", "skipped"]
    assert on_disk["state"] == "done" and on_disk["topics"] == []


def test_nothing_found_anywhere_fails_the_job_with_a_sentence(tmp_vault, monkeypatch):
    monkeypatch.setattr(wr, "web_research", _fake_loop({}, []))
    rec, stop = _started(_approved(2))
    dr._run(rec, stop)
    on_disk = dr.read(rec["id"])
    assert on_disk["state"] == "failed"
    assert on_disk["error"] == "nothing was found for any sub-question"


def test_the_approved_plan_is_checked_again_where_the_spend_happens(tmp_vault):
    with pytest.raises(dr.Refused):
        dr.start({**_approved(9)})                      # standard holds 8
    with pytest.raises(dr.Refused):
        dr.start({**_approved(1), "shape": "anything"})
    with pytest.raises(dr.Refused):
        dr.start({**_approved(0)})
    bad = _approved(1)
    bad["parts"][0]["held"] = "../../etc/passwd"
    assert dr._clean_plan(bad)["parts"][0]["held"] == ""


def test_one_research_at_a_time(tmp_vault):
    _started(_approved(1))
    with pytest.raises(dr.Refused, match="already running"):
        dr.start(_approved(1))


def test_only_the_parts_this_job_wrote_can_be_nucleated(tmp_vault, monkeypatch):
    monkeypatch.setattr(wr, "web_research", _fake_loop({"Sub-question 0?": 4}, []))
    monkeypatch.setattr(dr, "_ask", lambda m: "[]")
    rec, stop = _started(_approved(1))
    dr._run(rec, stop)
    part = dr.read(rec["id"])["parts"][0]["note"]
    with pytest.raises(dr.Refused):
        dr.mark_nucleated(rec["id"], ["Notes/Someone else's.md"])
    assert dr.mark_nucleated(rec["id"], [part, "Notes/x.md"]) == [part]
    assert dr.read(rec["id"])["nucleated"] is True


def test_own_turn_is_a_bank_of_its_own_seen_from_the_loops_read_threads():
    wr._SHARED.clear()
    wr._SHARED.bank["Q1"] = wr.Quote("https://chat.test", "chat", "w")
    with wr.own_turn() as mine:
        assert wr._turn() is mine and not mine.bank
        from silica.kernel.run_context import RunContext

        seen: list = []
        t = threading.Thread(target=RunContext.here().spawn(lambda: seen.append(wr._turn())))
        t.start()
        t.join()
        assert seen == [mine]
        wr._reset_turn()
    assert list(wr._SHARED.bank) == ["Q1"]
    wr._SHARED.clear()


def test_quote_ids_keep_the_quote_a_sentence_stands_on():
    bank = {"Q1": wr.Quote("https://a.test", "a", "w"), "Q2": wr.Quote("https://a.test", "b", "w"),
            "Q3": wr.Quote("https://b.test", "c", "w")}
    body = "One. [Q1, Q2] Two. [Q3][Q3] Three. [Q7]"
    bound, sources, audit = wr.bind_citations(body, [], bank, quote_ids=True)
    assert bound == "One. [Q1, Q2] Two. [Q3] Three."
    assert [u for u, _ in sources] == ["https://a.test", "https://b.test"]
    assert "1 marker(s)" in audit
    numbered, _, _ = wr.bind_citations(body, [], bank)
    assert numbered == "One. [1] Two. [2] Three."


def test_a_scholarly_hit_can_be_banked_from_its_abstract(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"doi": "https://doi.org/10.1/x", "display_name": "LNP study",
                                 "publication_year": 2021, "type": "article", "is_retracted": True,
                                 "abstract_inverted_index": {"Lipids": [0], "carry": [1], "mRNA.": [2]}}]}

    monkeypatch.setattr(wr.httpx, "get", lambda *a, **kw: _Resp())
    with wr.own_turn():
        hits = json.loads(wr.scholarly_search("lnp"))
        assert hits[0]["content"] == "Lipids carry mRNA." and hits[0]["retracted"] is True
        assert wr.remember("https://doi.org/10.1/x", "Lipids carry mRNA.", "what LNPs do").startswith("banked [Q1]")


def test_a_long_sub_question_names_its_file_without_a_dangling_comma_or_parenthesis():
    q = ("What are the core components of mRNA vaccine formulations (mRNA construct, lipid nanoparticle "
         "delivery system, and excipients)?")
    assert dr._part_name(q) == "What are the core components of mRNA vaccine formulations"
    assert dr._part_name("[04:12] Is it true?") == "Is it true"


def test_nucleate_holds_a_document_silica_wrote_from_the_vault_back(tmp_vault):
    """ADR-0158 point 11: a document written from the vault is not a new source;
    /nucleate says so and runs on it only with --anyway (Open 10's proposal)."""
    from silica.sources import ingest

    tmp_vault.note("Documents/Book.md", "---\ntitle: Book\nwritten_from: vault\n---\nChapter one.\n")
    tmp_vault.note("Inbox/Fresh.md", "---\ntitle: Fresh\n---\nNew material.\n")
    kept, held = ingest.hold_written(["Documents/Book.md", "Inbox/Fresh.md", "code.py"])
    assert kept == ["Inbox/Fresh.md", "code.py"] and held == ["Documents/Book.md"]
    said: list[str] = []

    class _Out:
        def print(self, text, **kw):
            said.append(str(text))

    assert ingest.nucleate(["Documents/Book.md"], console=_Out()) == ""
    assert "written by Silica from your notes" in said[-1] and "--anyway" in said[-1]
    assert "written by Silica" in ingest.run_injector(["Documents/Book.md"], "Notes")["error"]


def test_the_notes_the_parts_became_are_found_by_their_sources(tmp_vault, monkeypatch):
    monkeypatch.setattr(wr, "web_research", _fake_loop({"Sub-question 0?": 4}, []))
    monkeypatch.setattr(dr, "_ask", lambda m: "[]")
    rec, stop = _started(_approved(1))
    dr._run(rec, stop)
    part = Path(dr.read(rec["id"])["parts"][0]["note"]).name
    tmp_vault.note("Notes/Born of it.md", f'---\nsources:\n  - "{part}"\n---\nA claim.\n')
    tmp_vault.note("Notes/Unrelated.md", '---\nsources:\n  - "Other.md"\n---\nB.\n')
    tmp_vault.note("Notes/Mine, patched.md", f'---\nAI: partial\nsources:\n  - "{part}"\n---\nC.\n')
    tmp_vault.note("Notes/Notes.md", f'---\nsources:\n  - "{part}"\n---\nhub\n')
    assert dr.admitted(rec["id"]) == ["Notes/Born of it.md"]


def test_the_local_model_is_the_provider_not_a_base_url_pinned_beside_a_hosted_one(monkeypatch):
    """ADR-0158 point 6. The first live run had openrouter with a local
    llama-server URL pinned as its fallback, and was refused as local."""
    from silica.config import CONFIG
    from silica.ui.web.research_routes import local_model

    monkeypatch.setattr(CONFIG, "provider", "openrouter")
    monkeypatch.setattr(CONFIG, "provider_base_url", "http://127.0.0.1:1237/v1")
    assert not local_model()
    monkeypatch.setattr(CONFIG, "provider", "custom")
    assert local_model()
    monkeypatch.setattr(CONFIG, "provider", "ollama")
    assert local_model()
