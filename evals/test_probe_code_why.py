"""Mechanical parts of the code-why gate: the ones that decide whether the
number means anything. The arms themselves need an LLM and are not tested here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evals import probe_code_why as P


def test_verdict_table_matches_the_pre_registration():
    assert P._verdict(3, 0) == "PASS"
    assert P._verdict(7, 1) == "PASS"
    assert P._verdict(1, 0) == "KILL"
    assert P._verdict(0, 0) == "KILL"
    assert P._verdict(2, 0) == "GREY"          # +2 does not decide
    assert P._verdict(5, 2) == "CONFOUNDED"    # lookup moved: B just has more context
    assert P._verdict(5, -2) == "CONFOUNDED"
    assert P._verdict(0, 3) == "CONFOUNDED"    # confound beats kill: the run is void


def test_gate_vault_and_vendored_repos_are_unreachable():
    # the gate is void if arm A can read arm B's channel
    assert P._excluded(".silica/code-why-gate/memory/x.md")
    assert P._excluded("docs/repos/cognee/README.md")
    # ...or if it can read the golds out of the harness, which it did on the
    # first smoke run: "based on the content of evals/probe_code_why.py"
    assert P._excluded("evals/probe_code_why.py")
    assert P._excluded("evals/test_probe_code_why.py")
    assert not P._excluded("evals/probe_ppr_phase2.py")
    assert not P._excluded("docs/spec-code-lane.md")
    assert not P._excluded("silica/kernel/codetree.py")


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd",
                                 ".silica/code-why-gate/memory/x.md",
                                 "docs/repos/cognee/README.md"])
def test_file_tools_refuse_out_of_bounds_paths(bad):
    assert P.probe_read(path=bad)["status"] == "error"
    assert P.probe_grep(pattern="x", path=bad)["status"] == "error"


def test_probe_read_and_grep_work_on_a_real_file():
    r = P.probe_read(path="silica/kernel/codetree.py")
    assert r["status"] == "ok" and "why_for" in r["text"]
    g = P.probe_grep(pattern=r"def why_for", path="silica/kernel/codetree.py")
    assert g["status"] == "ok" and len(g["matches"]) == 1


def test_extract_paths_keeps_only_real_repo_paths(tmp_path):
    (tmp_path / "silica").mkdir()
    (tmp_path / "silica" / "m.py").write_text("x\n", encoding="utf-8")
    text = ("see `silica/m.py`, and silica/m.py. Also silica/ghost.py and "
            "https://example.com/silica/m.py")
    assert P.extract_paths(text, tmp_path) == ["silica/m.py"]


def test_backfill_round_trips_through_why_for(tmp_path):
    from silica.kernel import codetree

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "silica").mkdir()
    (tmp_path / "silica" / "m.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=tmp_path, check=True)

    src = tmp_path / "notes" / "memo.md"
    src.parent.mkdir()
    src.write_text("---\nname: memo\n---\n\nWe killed the walk in silica/m.py.\n",
                   encoding="utf-8")
    empty = tmp_path / "notes" / "nothing.md"
    empty.write_text("no paths here\n", encoding="utf-8")

    vault = tmp_path / "gate"
    res = P.backfill(vault, tmp_path, [src, empty])
    assert res == {"written": 1, "skipped_no_path": 1, "vault": str(vault)}

    hits, residue = codetree.why_for(vault, "silica", repo_root=tmp_path)
    assert residue == 0
    assert [(h.relation, h.bound_path, h.stale) for h in hits] == [
        ("member", "silica/m.py", False)]
    assert "We killed the walk" in (vault / "notes" / "memo.md").read_text(encoding="utf-8")


def test_backfill_note_paths_do_not_collide_on_stem(tmp_path):
    # 47 notes were silently lost to bare stems on the first real run
    a = tmp_path / "one" / "spec.md"
    b = tmp_path / "two" / "spec.md"
    for p in (a, b):
        p.parent.mkdir(parents=True)
        p.write_text("about silica/m.py\n", encoding="utf-8")
    (tmp_path / "silica").mkdir()
    (tmp_path / "silica" / "m.py").write_text("x\n", encoding="utf-8")

    vault = tmp_path / "gate"
    assert P.backfill(vault, tmp_path, [a, b])["written"] == 2
    assert len(list(vault.rglob("*.md"))) == 2


def test_arms_differ_only_by_the_code_lane():
    assert set(P._ARM_B_TOOLS) - set(P._ARM_A_TOOLS) == {"silica_code_why", "silica_recall"}
    assert set(P._ARM_A_TOOLS) - set(P._ARM_B_TOOLS) == set()


def test_question_set_matches_the_pre_registered_strata():
    strata = [q["stratum"] for q in P.QUESTIONS]
    assert strata.count("LOOKUP") == 8 and strata.count("WHY") == 7
    assert len({q["id"] for q in P.QUESTIONS}) == 15
    assert all(q["gold"].strip() for q in P.QUESTIONS)


def test_run_aggregates_and_verdicts_without_an_llm(tmp_path, monkeypatch):
    """The reporting plumbing, exercised before 30 real LLM calls depend on it.
    Arm B answers every WHY correctly, arm A none; LOOKUP is a tie."""
    import evals.longmemeval.runner as lme

    def fake_ask(model, question, tools):
        spec = next(q for q in P.QUESTIONS if q["q"] == question)
        right = spec["stratum"] == "LOOKUP" or tools == P._ARM_B_TOOLS
        return {"response": spec["gold"] if right else "I do not have that information.",
                "tools_used": ["probe_grep"] * (2 if right else 6),
                "iterations": 1, "budget_exhausted": False, "error": None}

    monkeypatch.setattr(P, "ask", fake_ask)
    monkeypatch.setattr(lme, "judge",
                        lambda m, t, q, gold, resp: resp.strip() == gold.strip())

    res = P.run("m", "m", tmp_path, tmp_path)
    assert res["strata"]["LOOKUP"] == {"n": 8, "A": 8, "B": 8, "delta": 0, "ungraded": 0}
    assert res["strata"]["WHY"] == {"n": 7, "A": 0, "B": 7, "delta": 7, "ungraded": 0}
    assert res["verdict"] == "PASS"
    # paired() is A-minus-B by argument order, so a positive delta here is B's win
    assert res["paired_overall"]["delta"] > 0
    assert len(res["questions"]) == 15


def test_excluded_files_are_pruned_from_the_walk_not_just_dirs():
    # pruning dirnames alone let the two harness FILES through: the first fix
    # for the gold leak did nothing until the walk filtered filenames too
    leak = P.probe_grep(pattern="monotone-decreasing")
    assert leak["matches"] == [] and leak["residue"] == 0
    assert P.probe_glob(pattern="evals/probe_code_why.py")["paths"] == []
    assert P.probe_glob(pattern="evals/probe_ppr_phase2.py")["paths"] != []


def test_arm_tools_are_registered(monkeypatch):
    # the liveness guard: run_agent drops unknown constraint names in silence,
    # which made arm B identical to arm A on the first smoke run
    P.assert_arms_live()
    from silica.tools import TOOLS
    monkeypatch.delitem(TOOLS, "silica_code_why")
    with pytest.raises(SystemExit, match="silica_code_why"):
        P.assert_arms_live()


def _eff_rows(pairs, why_delta=0, exhausted_a=0):
    return [{"stratum": "WHY",
             "A": {"tools_used": ["t"] * a, "budget_exhausted": i < exhausted_a},
             "B": {"tools_used": ["t"] * b, "budget_exhausted": False}}
            for i, (a, b) in enumerate(pairs)]


def test_efficiency_gate_thresholds():
    # 6 of 7 cheaper, median reduction well over 20% -> PASS
    rows = _eff_rows([(10, 5), (10, 4), (8, 4), (6, 3), (12, 6), (5, 9), (10, 5)])
    r = P.efficiency(rows, why_delta=0)
    assert r["b_cheaper_on"] == 6 and r["b_costlier_on"] == 1
    assert r["verdict"] == "PASS" and r["median_reduction"] >= 0.20

    # cheaper on only 4 of 7 -> below EFF_WIN_MIN, FAIL even with a big median
    assert P.efficiency(_eff_rows([(10, 1), (10, 1), (10, 1), (10, 1),
                                   (1, 10), (1, 10), (1, 10)]), 0)["verdict"] == "FAIL"

    # wins often but only barely -> median under 20%, FAIL
    assert P.efficiency(_eff_rows([(10, 9)] * 7), 0)["verdict"] == "FAIL"


def test_efficiency_is_void_when_arm_b_lost_accuracy():
    # cheaper AND wrong is not a win; the number must not be readable alone
    rows = _eff_rows([(10, 2)] * 7)
    assert P.efficiency(rows, why_delta=0)["verdict"] == "PASS"
    assert P.efficiency(rows, why_delta=-1)["verdict"] == "VOID"


def test_efficiency_reports_budget_exhaustion_per_arm():
    r = P.efficiency(_eff_rows([(24, 4)] * 7, exhausted_a=2), 0)
    assert r["budget_exhausted"] == {"A": 2, "B": 0}
    assert r["max_calls"] == {"A": 24, "B": 4}


def test_run_reports_the_efficiency_gate(tmp_path, monkeypatch):
    """The secondary gate rides on the same run, and its own thresholds."""
    import evals.longmemeval.runner as lme

    def fake_ask(model, question, tools):
        spec = next(q for q in P.QUESTIONS if q["q"] == question)
        right = spec["stratum"] == "LOOKUP" or tools == P._ARM_B_TOOLS
        return {"response": spec["gold"] if right else "no idea",
                "tools_used": ["probe_grep"] * (3 if tools == P._ARM_B_TOOLS else 10),
                "iterations": 1, "budget_exhausted": False, "error": None}

    monkeypatch.setattr(P, "ask", fake_ask)
    monkeypatch.setattr(lme, "judge",
                        lambda m, t, q, gold, resp: resp.strip() == gold.strip())
    e = P.run("m", "m", tmp_path, tmp_path)["efficiency"]
    assert e["verdict"] == "PASS" and e["b_cheaper_on"] == 7
    assert e["mean_calls"] == {"A": 10.0, "B": 3.0}


def test_why_stratum_is_not_greppable_from_the_tree():
    # the guard that runs 1 and 2 lacked: every WHY answer must live outside the
    # working tree, or arm A reaches it and a PASS is arithmetically impossible
    assert P.leak_check(P._repo_root()) == []
    assert all(q.get("fingerprints") for q in P.QUESTIONS if q["stratum"] == "WHY")
    assert not any(q.get("fingerprints") for q in P.QUESTIONS if q["stratum"] == "LOOKUP")


def test_leak_check_catches_a_planted_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "QUESTIONS", [
        {"id": "X1", "stratum": "WHY", "fingerprints": ["Zagaran", "v0.0.3"],
         "q": "?", "gold": "!"}])
    (tmp_path / "leaky.md").write_text("taken by Zagaran Inc., v0.0.3\n", encoding="utf-8")
    (tmp_path / "clean.md").write_text("Zagaran only\n", encoding="utf-8")
    assert P.leak_check(tmp_path) == [("X1", "leaky.md")]
