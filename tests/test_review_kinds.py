"""ADR-0165: /review is the one review queue.

One registry of kinds; a listing calls no model; a yes is one revertible run
per batch through the existing write path; a no is remembered.
"""
from __future__ import annotations

import pytest

from silica.kernel import review as queue
from silica.kernel.review import Kind, Proposal, proposal_id


def _said(out) -> str:
    return "\n".join(str(o) for objects, _ in out.lines for o in objects)


@pytest.fixture
def kinds(monkeypatch):
    """The real kinds, on a registry this test owns."""
    monkeypatch.setattr(queue, "KINDS", {})
    import importlib

    import silica.tools.review as pkg
    for name in ("aliases", "curate", "deferred", "organize", "stale"):
        importlib.reload(importlib.import_module(f"silica.tools.review.{name}"))
    return pkg.load()


@pytest.fixture
def no_model(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a listing called the model")
    monkeypatch.setattr("silica.model.llm.call_llm", refuse)
    monkeypatch.setattr("silica.model.ask.ask_json", refuse)


def test_adding_a_kind_is_a_file_in_the_package(kinds):
    import pkgutil

    import silica.tools.review as pkg
    files = {m.name for m in pkgutil.iter_modules(pkg.__path__)}
    assert files == {"aliases", "curate", "deferred", "organize", "stale"}
    assert set(kinds) == {"alias", "autolink", "orphan", "duplicate", "deferred", "move", "stale"}
    assert {k for k, v in kinds.items() if v.spends_model} == {"orphan", "duplicate"}
    assert {k for k, v in kinds.items() if not v.in_all} == {"stale"}


def test_an_id_outlives_the_listing_that_made_it():
    assert proposal_id("autolink", "A.md", "B.md") == proposal_id("autolink", "A.md", "B.md")
    assert proposal_id("autolink", "A.md", "B.md") != proposal_id("autolink", "B.md", "A.md")
    assert len(proposal_id("move", "x")) == 10


def test_listing_the_real_kinds_on_an_empty_vault_calls_no_model(tmp_vault, kinds, no_model):
    from silica.config import CONFIG

    assert queue.listing(CONFIG.vault_path) == []


def _toy(monkeypatch, tmp_vault, applied: dict[str, str | None] | None = None):
    """One kind that writes through the gate's journalled write."""
    from silica.kernel.write import gate

    def apply(vault, props):
        out: dict[str, str | None] = {}
        for p in props:
            if (applied or {}).get(p.id):
                out[p.id] = applied[p.id]
                continue
            gate.journaled_write(p.data["path"], lambda t: t + "\nyes\n", source="toy",
                                 run=gate.current_run() or "")
            out[p.id] = None
        return out

    monkeypatch.setattr(queue, "KINDS", {})
    queue.register(Kind("toy", "Toys", lambda vault: queue.offered(vault, "toy"), apply))


def test_a_yes_is_one_run_for_the_batch_and_its_revert_takes_all_of_it_back(tmp_vault, monkeypatch):
    from silica.config import CONFIG

    vault = CONFIG.vault_path
    a, b = tmp_vault.note("A.md", "# A\n"), tmp_vault.note("B.md", "# B\n")
    _toy(monkeypatch, tmp_vault)
    props = [Proposal(id=proposal_id("toy", n), kind="toy", title=n, why="w", notes=(n,), data={"path": n})
             for n in ("A.md", "B.md")]
    queue.offer(vault, "toy", props)

    res = queue.accept(vault, queue.pick(vault, ["toy"]))
    assert res["run"] and len(res["applied"]) == 2 and not res["failed"]
    assert tmp_vault.read(a).endswith("yes\n") and tmp_vault.read(b).endswith("yes\n")
    assert queue.listing(vault) == []                       # a yes leaves the queue
    (batch,) = queue.batches(vault)
    assert batch["run"] == res["run"] and batch["count"] == 2 and not batch["reverted"]

    queue.revert(vault, res["run"])
    assert tmp_vault.read(a) == "# A\n" and tmp_vault.read(b) == "# B\n"
    assert queue.batches(vault)[0]["reverted"]
    assert queue.revert(vault, "not-a-batch") is None       # only this queue's runs


def test_what_did_not_land_stays_with_its_reason_and_a_batch_that_wrote_nothing_is_no_run(tmp_vault, monkeypatch):
    from silica.config import CONFIG

    vault = CONFIG.vault_path
    tmp_vault.note("A.md", "# A\n")
    p = Proposal(id=proposal_id("toy", "A.md"), kind="toy", title="A", why="w", notes=("A.md",),
                 data={"path": "A.md"})
    _toy(monkeypatch, tmp_vault, applied={p.id: "refused"})
    queue.offer(vault, "toy", [p])
    res = queue.accept(vault, [p])
    assert res["run"] is None and res["failed"] == [(p, "refused")]
    assert [x.id for x in queue.listing(vault)] == [p.id]
    assert queue.batches(vault) == []


def test_a_no_is_remembered_when_the_command_proposes_it_again(tmp_vault, monkeypatch):
    from silica.config import CONFIG

    vault = CONFIG.vault_path
    _toy(monkeypatch, tmp_vault)
    p = Proposal(id=proposal_id("toy", "A.md"), kind="toy", title="A", why="w", notes=("A.md",),
                 data={"path": "A.md"})
    queue.offer(vault, "toy", [p])
    queue.dismiss(vault, queue.pick(vault, [p.id[:6]]))      # a prefix names it, as /revert's do
    queue.offer(vault, "toy", [p])                           # the next dry run finds it again
    assert queue.listing(vault) == []


def test_all_passes_over_a_kind_a_person_must_name(tmp_vault, monkeypatch):
    from silica.config import CONFIG

    vault = CONFIG.vault_path
    _toy(monkeypatch, tmp_vault)
    queue.register(Kind("named", "Named", lambda v: [Proposal(id="n" * 10, kind="named", title="N", why="w")],
                        lambda v, ps: {}, in_all=False))
    queue.offer(vault, "toy", [Proposal(id="t" * 10, kind="toy", title="T", why="w")])
    assert [p.kind for p in queue.pick(vault, ["all"])] == ["toy"]
    assert [p.kind for p in queue.pick(vault, ["named"])] == ["named"]
    assert [p.kind for p in queue.pick(vault, ["nnnn"])] == ["named"]


def test_an_autolink_is_offered_only_where_a_note_names_the_other_and_lands_revertibly(tmp_vault, kinds):
    from silica.config import CONFIG
    from silica.kernel.report.curator import CurationItem, CurationPlan
    from silica.tools.review.curate import offer_plan

    vault = CONFIG.vault_path
    src = tmp_vault.note("Notes/Gradient.md", "# Gradient\n\nThe Loss surface is what descent walks.\n")
    tmp_vault.note("Notes/Loss.md", "# Loss\n\nA number to make small.\n")
    tmp_vault.note("Notes/Kernel.md", "# Kernel\n\nNothing in common in words.\n")
    offer_plan(vault, CurationPlan(items=[
        CurationItem("autolink", "Notes/Loss.md", "Notes/Gradient.md", 3.0, "co-occurrence: loss"),
        CurationItem("autolink", "Notes/Kernel.md", "Notes/Loss.md", 2.0, "co-occurrence: number"),
    ]))
    (p,) = [x for x in queue.listing(vault) if x.kind == "autolink"]
    assert p.title == "Link Loss from Gradient" and "[[Loss]]" in p.preview   # the other way round
    res = queue.accept(vault, [p])
    assert res["run"] and "[[Loss]]" in tmp_vault.read(src)
    queue.revert(vault, res["run"])
    assert "[[Loss]]" not in tmp_vault.read(src)


def test_a_move_joins_the_batch_run_and_a_taken_destination_refuses_that_note_alone(tmp_vault, kinds):
    from pathlib import Path

    from silica.config import CONFIG
    from silica.tools.review.organize import offer_moves

    vault = CONFIG.vault_path
    tmp_vault.note("A.md", "# A\n")
    tmp_vault.note("B.md", "# B\n")
    tmp_vault.note("Done/B.md", "# B elsewhere\n")
    offer_moves(vault, [{"from": "A.md", "to": "Done/A.md", "evidence": "keyword"},
                        {"from": "B.md", "to": "Done/B.md", "evidence": "keyword"}])
    res = queue.accept(vault, queue.pick(vault, ["move"]))
    assert [p.data["from"] for p in res["applied"]] == ["A.md"]
    assert "already exists" in res["failed"][0][1]
    assert (Path(vault) / "Done/A.md").exists()
    queue.revert(vault, res["run"])
    assert (Path(vault) / "A.md").exists() and not (Path(vault) / "Done/A.md").exists()


def test_alias_groups_wait_after_the_dry_run_and_a_yes_makes_no_model_call(tmp_vault, kinds, no_model, monkeypatch):
    from silica.config import CONFIG
    from silica.tools import aliases as tool
    from silica.tools.review.aliases import offer_groups

    vault = CONFIG.vault_path
    tmp_vault.note("Machine learning.md", "# Machine learning\n")
    offer_groups(vault, {"Machine learning": ["ML"]}, {"machine learning": "Machine learning.md"})
    seen = {}
    monkeypatch.setattr(tool, "write_aliases", lambda groups, paths: (seen.update(groups) or ({}, [])))
    (p,) = queue.listing(vault)
    assert p.kind == "alias" and p.preview == "aliases: + ML"
    queue.accept(vault, [p])
    assert seen == {"Machine learning": ["ML"]}


def test_a_deferred_bundle_is_a_proposal_its_no_flushes_it(tmp_vault, kinds):
    from silica.config import CONFIG
    from silica.kernel.write.deferred import get_deferred_store

    vault = CONFIG.vault_path
    get_deferred_store().put("h" * 64, "inbox/a.md", "Dir", None,
                             [{"op": "write", "heading": "GPU", "path": "Dir/GPU.md"}],
                             rejection_reasons={"Dir/GPU.md": "too generic"})
    (p,) = queue.listing(vault)
    assert p.kind == "deferred" and p.why == "too generic" and p.notes == ("Dir/GPU.md",)
    queue.dismiss(vault, [p])
    assert get_deferred_store().get("h" * 64) is None


def test_the_command_lists_by_kind_and_refuses_a_yes_in_the_demo(tmp_vault, kinds, monkeypatch):
    from silica.commands import dispatch
    from silica.config import CONFIG

    queue.offer(CONFIG.vault_path, "move", [Proposal(
        id=proposal_id("move", "A.md", "Done/A.md"), kind="move", title="Move A to Done", why="rules",
        notes=("A.md",), data={"from": "A.md", "to": "Done/A.md"})])
    said = _said(dispatch("/review", []))
    assert "Moves to a folder" in said and "Move A to Done" in said
    monkeypatch.setattr("silica.config.demo", lambda: True)
    out = dispatch("/review apply all", [])
    assert out.error and "demo" in out.error


def test_the_panel_lists_lands_a_yes_and_sends_a_model_yes_to_the_chat(tmp_vault, kinds):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.config import CONFIG
    from silica.ui.web import server

    api, vault = TestClient(server.app), CONFIG.vault_path
    tmp_vault.note("A.md", "# A\n")
    move = Proposal(id=proposal_id("move", "A.md", "Done/A.md"), kind="move", title="Move A to Done",
                    why="rules", notes=("A.md",), data={"from": "A.md", "to": "Done/A.md"})
    orphan = Proposal(id=proposal_id("orphan", "A.md"), kind="orphan", title="Connect A",
                      why="nothing links to it", notes=("A.md",), data={"target": "A.md"})
    queue.offer(vault, "move", [move])
    queue.offer(vault, "orphan", [orphan])
    d = api.get("/metrics/review").json()
    assert {k["name"]: len(k["items"]) for k in d["kinds"]}["move"] == 1 and d["batches"] == []
    refused = api.post("/metrics/review/apply", json={"ids": [move.id, orphan.id]})
    assert refused.status_code == 409 and refused.json()["code"] == "in_chat"
    landed = api.post("/metrics/review/apply", json={"ids": [move.id]}).json()
    assert landed["applied"] == [move.id] and landed["run"]
    assert api.get("/metrics/review").json()["batches"][0]["run"] == landed["run"]
    assert api.post("/metrics/review/revert", json={"run": landed["run"]}).json()["restored"]
    assert api.post("/metrics/review/revert", json={"run": "nope"}).status_code == 404
    assert api.post("/metrics/review/dismiss", json={"ids": [orphan.id]}).json()["dismissed"] == [orphan.id]
    assert api.post("/metrics/review/apply", json={"ids": [orphan.id]}).status_code == 404
