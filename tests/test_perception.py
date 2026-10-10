# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Answer-time perception (silica/kernel/recall/perception.py).

perceive() is the single assembly of recalled memory into model context —
the LongMemEval harness consumes this same function, so these tests cover the
product behavior the eval numbers are attributed to: per-note query-densest
window, rank/evidence/date headers, facts-first episodic block, degraded legs.
All offline: co-occurrence retrieval only, no embedder, no reranker.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _bind(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CONFIG/DRIVER at a fresh fs vault; singletons reset per test."""
    import silica.driver
    import silica.kernel.recall.cooccurrence as cooc_mod
    import silica.kernel.recall.embed as embed_mod
    from silica.config import CONFIG

    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "memory_vault", str(vault))  # coincident: lane abstains
    monkeypatch.setattr(silica.driver, "_driver", None)
    embed_mod.clear()
    cooc_mod.clear()


def _write(rel: str, date: str, body: str) -> None:
    from silica.driver import DRIVER

    DRIVER.create(rel, f'---\ndate: "{date}"\n---\n\n{body}\n')


def _index() -> None:
    from silica.tools.graph import silica_cooccurrence_refresh

    silica_cooccurrence_refresh(force=True)


LONG_BODY = ("filler chatter " * 400) + "the yoga class is on Tuesday evening " \
            + ("more filler " * 400)


def test_best_window_is_public():
    # The harness used to import the private name; the seam is public now.
    from silica.kernel.recall.rerank import _best_window, best_window

    assert best_window is _best_window


def test_perceive_windows_bodies_under_rank_evidence_date_headers(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", LONG_BODY)
    _write("sessions/b.md", "2026-02-02", "short note about cooking pasta")
    _index()
    from silica.kernel.recall.perception import perceive

    p = perceive("when is my yoga class?", now="2026-05-01", k=2,
                 window_chars=200, use_embedder=False)
    assert p.blocks, "cooccur leg should retrieve the yoga note"
    top = p.blocks[0]
    assert top.path == "sessions/a"
    assert "yoga class is on Tuesday" in top.excerpt      # query-densest window
    assert len(top.excerpt) <= 200                        # the wall was cut
    assert "date:" not in top.excerpt                     # frontmatter stripped
    assert top.evidence                                   # per-leg provenance survives

    ctx = p.render()
    assert f"[#1 | sessions/a | {top.evidence} | dated 2026-01-01]" in ctx  # G1: path anchors the block
    assert "yoga class is on Tuesday" in ctx


def test_perceive_demotes_contested_note_last_with_marker(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    from silica.driver import DRIVER

    DRIVER.create(
        "sessions/bad.md",
        '---\ndate: "2026-01-01"\ncontested: true\n'
        'contradictions:\n  - "flagged: wrong day (by user, 2026-05-01)"\n'
        "---\n\nyoga on Monday\n",
    )
    DRIVER.create("sessions/good.md", '---\ndate: "2026-02-02"\n---\n\nyoga on Tuesday\n')
    from silica.kernel.recall.perception import perceive

    # paths= assembles the given notes in order (bad first): the contested one
    # must be demoted behind the clean note regardless of input order.
    p = perceive("yoga?", now="2026-05-01",
                 paths=["sessions/bad", "sessions/good"], use_embedder=False)
    assert [b.path for b in p.blocks] == ["sessions/good", "sessions/bad"]
    assert p.blocks[-1].contested and "wrong day" in p.blocks[-1].contested
    assert p.blocks[0].contested is None
    assert "contested" in p.render().lower()  # marker reaches the answer context


def test_render_flat_returns_whole_bodies_without_rank_headers(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", LONG_BODY)
    _index()
    from silica.kernel.recall.perception import perceive

    p = perceive("when is my yoga class?", now="2026-05-01", k=1,
                 window_chars=200, use_embedder=False)
    flat = p.render(windowed=False)
    assert "[dated 2026-01-01]" in flat
    assert "[#1" not in flat
    assert "filler chatter" in flat and "more filler" in flat  # body uncut


# --- multi-window perception (multi-window spec 2026-07-15) -----------------

# Gold tokens (Tuesday/Thursday) sit BEFORE the query terms: on density ties the
# earliest window wins, so terms-first phrasing would cut the trailing gold — the
# adjacency risk the spec's arm A/B comparison measures, not this unit's concern.
TWO_FACT_BODY = ("filler chatter " * 40) + "on Tuesday evening we go to the yoga class " \
                + ("filler chatter " * 40) + "on Thursday evening we moved the yoga class " \
                + ("filler chatter " * 40)


def test_perceive_multi_window_excerpt_joins_with_elision_marker(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", TWO_FACT_BODY)
    _index()
    from silica.kernel.recall.perception import perceive

    p = perceive("when is my yoga class?", now="2026-05-01", k=1,
                 window_chars=150, windows=2, use_embedder=False)
    ex = p.blocks[0].excerpt
    assert "\n[…]\n" in ex
    assert "Tuesday" in ex and "Thursday" in ex
    assert ex.index("Tuesday") < ex.index("Thursday")  # document order survives
    assert len(ex) <= 2 * 150 + len("\n[…]\n")


def test_single_window_arm_emits_no_elision_marker(tmp_path, monkeypatch):
    # windows=1 is no longer the default (see the defaults test below), but the
    # single-window contract still holds for the eval arms that request it:
    # no marker, excerpt == best_window of the body.
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", TWO_FACT_BODY)
    _index()
    from silica.kernel.recall.perception import perceive
    from silica.kernel.recall.rerank import best_window

    p = perceive("when is my yoga class?", now="2026-05-01", k=1,
                 window_chars=150, windows=1, use_embedder=False)
    b = p.blocks[0]
    assert "[…]" not in b.excerpt
    assert b.excerpt == best_window(b.body, "when is my yoga class?", 150)


def test_render_defaults_are_the_decided_window_grid():
    """The 3x1000 grid is a measured decision, not a preference: it beat 1x3000
    on answer accuracy 0.520 vs 0.427 over 150 paired LME questions (McNemar
    p=0.0336, bench/ab_win_*.metrics.json). Pinned so a silent revert to a wide
    single window has to break a test and re-argue the measurement."""
    from silica.kernel.recall import perception

    assert (perception.DEFAULT_WINDOWS, perception.WINDOW_CHARS) == (3, 1000)
    assert perception.DEFAULT_K == 15  # probe_recall_rank: the rank tail carries gold


def test_perceive_multi_window_short_body_passes_whole(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "short note about the yoga class")
    _index()
    from silica.kernel.recall.perception import perceive

    p = perceive("yoga class?", now="2026-05-01", k=1,
                 window_chars=200, windows=2, use_embedder=False)
    b = p.blocks[0]
    assert b.excerpt == b.body
    assert "[…]" not in b.excerpt


def _seed_fact(key="user.dog.name", text="My dog is named Zephyr",
               run_id="s1", seen="2026-01-01") -> None:
    from silica.kernel.recall.episodic import EpisodicStore

    EpisodicStore().capture([{"key": key, "text": text}], run_id=run_id, seen=seen)


def test_facts_block_first_by_default_last_on_request(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    _seed_fact()
    from silica.kernel.recall.perception import perceive

    p = perceive("What is my dog's name?", now="2026-05-01", k=5,
                 use_embedder=False, episodic_ttl_days=0)
    assert p.facts_block.startswith("Personal memory:")
    assert "Zephyr" in p.facts_block
    assert p.fact_chains and p.fact_chains[0][0].runs == ["s1"]  # telemetry chain

    ctx = p.render()
    assert ctx.index("Personal memory:") < ctx.index("[#1")
    tail = p.render(facts_first=False)
    assert tail.index("[#1") < tail.index("Personal memory:")


def test_facts_from_a_previous_embedder_still_reach_the_context(tmp_path, monkeypatch):
    """perceive() names the configured embedder to the store, so a fact embedded
    by the previous model is scored lexically instead of being dropped by the
    floor on a cosine across two spaces."""
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    from silica.kernel.recall.episodic import EpisodicStore

    class _E:
        def __init__(self, model, vec):
            self.model, self._vec = model, vec

        def embed(self, texts, query=False):
            return [list(self._vec) for _ in texts]

    EpisodicStore().capture([{"key": "user.dog.name", "text": "My dog is named Zephyr"}],
                            run_id="s1", seen="2026-01-01", embedder=_E("old", [1.0, 0.0]))
    import silica.agent.providers as providers

    monkeypatch.setattr(providers, "get_embedder", lambda _cfg: _E("new", [0.0, 1.0]))
    from silica.kernel.recall.perception import perceive

    p = perceive("What is my dog's name?", now="2026-05-01", k=5,
                 use_embedder=True, use_rerank=False, episodic_ttl_days=0)
    assert "Zephyr" in p.facts_block


def test_only_an_answer_that_delivers_facts_counts_as_their_use(tmp_path, monkeypatch):
    """C6: perceive() measures by default (evals, probes, replays must not keep
    facts alive); mark_used=True is the answer path, where the facts handed
    to the model count as used on `now`."""
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    _seed_fact()
    from silica.kernel.recall.episodic import EpisodicStore
    from silica.kernel.recall.perception import perceive

    perceive("What is my dog's name?", now="2026-05-01", k=5,
             use_embedder=False, episodic_ttl_days=0)
    assert EpisodicStore().live_facts()[0].last_used is None

    perceive("What is my dog's name?", now="2026-05-02", k=5,
             use_embedder=False, episodic_ttl_days=0, mark_used=True)
    assert EpisodicStore().live_facts()[0].last_used == "2026-05-02"


def test_silica_recall_marks_the_facts_it_delivers_as_used(tmp_path, monkeypatch):
    import datetime

    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    _seed_fact()
    from silica.kernel.recall.episodic import EpisodicStore
    from silica.tools.graph import silica_recall

    out = silica_recall(query="What is my dog's name?", k=5)
    assert out["facts"] == 1
    assert EpisodicStore().live_facts()[0].last_used == datetime.date.today().isoformat()


def test_empty_episodic_store_yields_no_facts_block(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    from silica.kernel.recall.perception import perceive

    p = perceive("What is my dog's name?", now="2026-05-01", k=5, use_embedder=False)
    assert p.facts_block == ""
    assert "Personal memory" not in p.render()


def test_with_facts_false_skips_episodic_recall(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "we talked about my dog at the park")
    _index()
    _seed_fact()
    from silica.kernel.recall.perception import perceive

    p = perceive("What is my dog's name?", now="2026-05-01", k=5,
                 use_embedder=False, with_facts=False)
    assert p.facts_block == "" and not p.fact_hits


def test_paths_override_skips_retrieval_keeps_order(tmp_path, monkeypatch):
    # --stuff arm: assemble the given notes in order, no index needed at all.
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "alpha body")
    _write("sessions/b.md", "2026-02-02", "beta body")
    from silica.kernel.recall.perception import perceive

    p = perceive("anything", now="2026-05-01", use_embedder=False,
                 paths=["sessions/b", "sessions/a"])
    assert [b.path for b in p.blocks] == ["sessions/b", "sessions/a"]
    assert all(b.evidence == "" for b in p.blocks)
    ctx = p.render()
    assert "[#1 | sessions/b | dated 2026-02-02]" in ctx   # no evidence segment, no double pipe
    assert "[#2 | sessions/a | dated 2026-01-01]" in ctx


def test_note_without_frontmatter_does_not_crash_perceive(tmp_path, monkeypatch):
    """A body-only note (no frontmatter) must assemble cleanly. Product notes
    written by the FSM write path can lack frontmatter; frontmatter.split then
    returns data=None and _read_dated_body used to crash on data.get (found by
    the LoCoMo e2e leg: perceive died mid-run at question 173/199)."""
    _bind(tmp_path / "v", monkeypatch)
    from silica.driver import DRIVER
    DRIVER.create("memory/plain.md", "just a body, no frontmatter at all\n")
    from silica.kernel.recall.perception import perceive

    p = perceive("anything", now="2026-05-01", use_embedder=False,
                 paths=["memory/plain"])
    assert [b.path for b in p.blocks] == ["memory/plain"]
    ctx = p.render()
    assert "just a body" in ctx
    assert "[#1 | memory/plain]" in ctx   # no date segment, no crash (G1: path present)


def test_unreadable_paths_are_skipped_rank_stays_dense(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "alpha body")
    from silica.kernel.recall.perception import perceive

    p = perceive("anything", now="2026-05-01", use_embedder=False,
                 paths=["missing/nope", "sessions/a"])
    assert [b.path for b in p.blocks] == ["sessions/a"]
    assert "[#1 | sessions/a | dated 2026-01-01]" in p.render()


def test_empty_bodied_note_is_dropped_not_rendered_as_a_bare_header(tmp_path, monkeypatch):
    """A retrieved note with no body used to occupy a "[#n | ...]" header and
    nothing else — measured on a real vault as rank 15 of a k=15 recall."""
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/hollow.md", "2026-01-01", "   \n\n")
    _write("sessions/a.md", "2026-02-02", "alpha body")
    from silica.kernel.recall.perception import perceive

    p = perceive("anything", now="2026-05-01", use_embedder=False,
                 paths=["sessions/hollow", "sessions/a"])
    assert [b.path for b in p.blocks] == ["sessions/a"]
    assert "[#2" not in p.render()  # ranks stay dense, no empty trailing block


def test_silica_recall_tool_returns_context_and_paths(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", LONG_BODY)
    _index()
    from silica.tools.graph import silica_recall

    out = silica_recall(query="when is my yoga class?", k=5)
    assert "yoga class is on Tuesday" in out["context"]
    assert out["notes"] == ["sessions/a"]
    assert out["facts"] == 0


def test_use_recall_weights_false_ignores_populated_store(tmp_path, monkeypatch):
    """Default off: a populated recall_weights store must not change output."""
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "short note about cooking pasta")
    _write("sessions/b.md", "2026-02-02", "short note about hiking trails")
    _index()
    from evals import recall_weights
    from silica.kernel.recall.perception import perceive

    recall_weights.bump(["sessions/b"])  # store populated, flag stays off
    p = perceive("pasta", now="2026-05-01", k=2, use_embedder=False)
    assert not any("recall:" in b.evidence for b in p.blocks)


def test_use_recall_weights_true_resurfaces_bumped_note(tmp_path, monkeypatch):
    """Flag on: a bumped note surfaces with recall: evidence via the RRF leg."""
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "short note about cooking pasta")
    _write("sessions/b.md", "2026-02-02", "short note about hiking trails")
    _index()
    from evals import recall_weights
    from evals.recall_arms import perceive

    recall_weights.bump(["sessions/b"])
    p = perceive("pasta", now="2026-05-01", k=2, use_embedder=False,
                 improve=True)
    assert any(b.path == "sessions/b" and "recall:" in b.evidence for b in p.blocks)


# --- G1: section chain in the block header (offline-signals-map §3) ---

def test_section_chain_anchors_window_under_its_headings(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    body = ("# Training\n" + "intro filler " * 40
            + "\n## Gradients\n" + "descent step size chosen " * 60
            + "\n## Schedule\n" + "cosine decay " * 40)
    _write("sessions/a.md", "2026-01-01", body)
    from silica.kernel.recall.perception import perceive

    p = perceive("descent step size", now="2026-05-01", use_embedder=False,
                 window_chars=200, paths=["sessions/a"])
    assert p.blocks[0].section == "Training > Gradients"
    assert "| sec: Training > Gradients |" in p.render() or \
           "| sec: Training > Gradients]" in p.render()


def test_section_chain_pops_siblings_and_caps_depth():
    from silica.kernel.recall.perception import _section_chain
    body = ("# A\n## B\ntext\n## C\n### D\nhere")
    off = body.index("here")
    assert _section_chain(body, off) == "A > C > D"          # B popped by C
    assert _section_chain(body, off, depth=2) == "C > D"     # deepest levels win


def test_headingless_note_renders_without_sec_segment(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "plain prose with no headings at all")
    from silica.kernel.recall.perception import perceive

    p = perceive("prose headings", now="2026-05-01", use_embedder=False,
                 paths=["sessions/a"])
    assert p.blocks[0].section == ""
    assert "| sec:" not in p.render()


# --- G6: study order (prereq-first blocks + builds-on tokens, V2 RefD) ---

def _study_vault(tmp_path, monkeypatch, prereqs):
    _bind(tmp_path / "v", monkeypatch)
    import silica.kernel.report.learner as learner
    monkeypatch.setattr(learner, "prerequisites_map", lambda: prereqs)


def test_study_order_puts_prerequisite_first_and_annotates(tmp_path, monkeypatch):
    _study_vault(tmp_path, monkeypatch,
                 {"sessions/deep": ["sessions/basics"]})
    _write("sessions/deep.md", "2026-01-02", "backprop chains the gradients")
    _write("sessions/basics.md", "2026-01-01", "derivatives measure change")
    from evals.recall_arms import perceive

    p = perceive("gradients", now="2026-05-01", use_embedder=False,
                 paths=["sessions/deep", "sessions/basics"], study=True)
    assert [b.path for b in p.blocks] == ["sessions/basics", "sessions/deep"]
    assert p.blocks[1].builds_on == "basics"
    assert "| builds-on: basics" in p.render()


def test_study_order_never_changes_membership_and_default_off(tmp_path, monkeypatch):
    _study_vault(tmp_path, monkeypatch,
                 {"sessions/deep": ["sessions/basics"]})
    _write("sessions/deep.md", "2026-01-02", "backprop chains the gradients")
    _write("sessions/basics.md", "2026-01-01", "derivatives measure change")
    from evals.recall_arms import perceive

    off = perceive("gradients", now="2026-05-01", use_embedder=False,
                   paths=["sessions/deep", "sessions/basics"])
    on = perceive("gradients", now="2026-05-01", use_embedder=False,
                  paths=["sessions/deep", "sessions/basics"], study=True)
    assert [b.path for b in off.blocks] == ["sessions/deep", "sessions/basics"]
    assert {b.path for b in on.blocks} == {b.path for b in off.blocks}
    assert all(not b.builds_on for b in off.blocks)   # annotation is study-only


def test_study_order_keeps_contested_demoted(tmp_path, monkeypatch):
    # The contested prerequisite may NOT ride its didactic rank back above
    # clean notes: distrust outranks reading order.
    _study_vault(tmp_path, monkeypatch,
                 {"sessions/deep": ["sessions/basics"]})
    _write("sessions/deep.md", "2026-01-02", "backprop chains the gradients")
    from silica.driver import DRIVER
    DRIVER.create(
        "sessions/basics.md",
        '---\ndate: "2026-01-01"\ncontested: true\n'
        'contradictions:\n  - "flagged: superseded (by user, 2026-05-01)"\n'
        "---\n\nderivatives measure change\n")
    from evals.recall_arms import perceive

    p = perceive("gradients", now="2026-05-01", use_embedder=False,
                 paths=["sessions/deep", "sessions/basics"], study=True)
    assert [b.path for b in p.blocks] == ["sessions/deep", "sessions/basics"]


def test_study_order_survives_unavailable_prereq_map(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    import silica.kernel.report.learner as learner

    def _boom():
        raise RuntimeError("no cooccur depth")

    monkeypatch.setattr(learner, "prerequisites_map", _boom)
    _write("sessions/a.md", "2026-01-01", "alpha body")
    from evals.recall_arms import perceive

    p = perceive("alpha", now="2026-05-01", use_embedder=False,
                 paths=["sessions/a"], study=True)
    assert [b.path for b in p.blocks] == ["sessions/a"]


# --- G2: orientation block (vault map) as an opt-in perception arm ---

def test_orient_prepends_vault_map_and_default_off(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "alpha body")
    import silica.kernel.recall.vault_map as vm
    monkeypatch.setattr(vm, "build_vault_map", lambda: "## Vault map\n- Notes: 1")
    from evals.recall_arms import perceive

    off = perceive("alpha", now="2026-05-01", use_embedder=False,
                   paths=["sessions/a"])
    on = perceive("alpha", now="2026-05-01", use_embedder=False,
                  paths=["sessions/a"], orient=True)
    assert "Vault map" not in off.render()          # default off: byte-identical
    assert on.render().startswith("## Vault map")   # the map frames the evidence
    assert "alpha body" in on.render()


def test_orient_fails_open_when_map_unavailable(tmp_path, monkeypatch):
    _bind(tmp_path / "v", monkeypatch)
    _write("sessions/a.md", "2026-01-01", "alpha body")
    import silica.kernel.recall.vault_map as vm

    def _boom():
        raise RuntimeError("no cooccur index")

    monkeypatch.setattr(vm, "build_vault_map", _boom)
    from evals.recall_arms import perceive

    p = perceive("alpha", now="2026-05-01", use_embedder=False,
                 paths=["sessions/a"], orient=True)
    assert "alpha body" in p.render()               # answering never blocks


# --- 2026-09-22 study-plan replay fixes (docs/evaluation/study-plan-rubric-2026-09-22.md)

def test_perceive_drops_a_byte_identical_body_and_keeps_the_shallower_path(tmp_path, monkeypatch):
    """A mineru intermediate (`x/hybrid_auto/x.md`) is the same file as `x.md`:
    on the test vault it took 2 of 15 slots with identical excerpts."""
    _bind(tmp_path / "v", monkeypatch)
    body = "yoga on Tuesday, then cooking pasta with friends"
    _write("Inbox/x/hybrid_auto/x.md", "2026-01-01", body)
    _write("Inbox/x.md", "2026-01-01", body)
    _write("Inbox/other.md", "2026-01-02", "yoga on Monday")
    from silica.kernel.recall.perception import perceive

    p = perceive("yoga?", now="2026-05-01",
                 paths=["Inbox/x/hybrid_auto/x", "Inbox/x", "Inbox/other"], use_embedder=False)
    assert [b.path for b in p.blocks] == ["Inbox/x", "Inbox/other"]


def test_perceive_marks_wikilinks_whose_target_does_not_exist(tmp_path, monkeypatch):
    """The model copied five [[links]] it saw inside note bodies into its answer;
    none of them named a note. The brackets stay (the model must know the name
    is not a note), embeds are untouched, and a legend leads the context."""
    _bind(tmp_path / "v", monkeypatch)
    _write("a.md", "2026-01-01", "see [[b]] and [[Ghost Note]] and ![[pic.png]] and [[b|alias]]")
    _write("b.md", "2026-01-01", "the b note")
    from silica.kernel.recall.perception import perceive

    p = perceive("b?", now="2026-05-01", paths=["a"], use_embedder=False)
    ex = p.blocks[0].excerpt
    assert "[[Ghost Note]] (no such note)" in ex
    assert "[[b]] (no" not in ex and "[[b|alias]] (no" not in ex
    assert "![[pic.png]] (no" not in ex
    assert p.render().startswith("[legend: ")
    # No unresolved link anywhere: no legend either, the context is unchanged.
    q = perceive("b?", now="2026-05-01", paths=["b"], use_embedder=False)
    assert "(no such note)" not in q.render() and not q.render().startswith("[legend")


def test_header_carries_the_declared_title_when_it_differs_from_the_path_stem(tmp_path, monkeypatch):
    """`Inbox/giovanni_castelli` says nothing; a declared `title:` is the identity
    the model attributed wrongly. The first H1 is NOT a title on this vault (372
    of 728 notes open with a section H1), so a note whose only H1 is a section,
    or whose title is its own name, gets no segment."""
    _bind(tmp_path / "v", monkeypatch)
    from silica.driver import DRIVER

    DRIVER.create("Inbox/giovanni_castelli.md",
                  '---\ndate: "2026-01-01"\ntitle: Tecnologie per i Big Data\n---\n\nyoga chapter\n')
    DRIVER.create("Yoga.md", '---\ndate: "2026-01-01"\ntitle: Yoga\n---\n\n# Yoga\n\nyoga note\n')
    _write("Plain.md", "2026-01-01", "# Definizione di yoga\n\nyoga without a declared title")
    from silica.kernel.recall.perception import perceive

    p = perceive("yoga", now="2026-05-01",
                 paths=["Inbox/giovanni_castelli", "Yoga", "Plain"], use_embedder=False)
    ctx = p.render()
    assert "[#1 | Inbox/giovanni_castelli | title: Tecnologie per i Big Data" in ctx
    assert "[#2 | Yoga |" in ctx and "title:" not in ctx.split("[#2")[1].split("]")[0]
    assert "title:" not in ctx.split("[#3")[1].split("]")[0]


def test_marked_link_does_not_make_a_whole_note_partial(tmp_path, monkeypatch):
    """`partial` told the model which notes to re-read; it was derived from
    excerpt != body, and the "(no such note)" marker edits the excerpt, so a
    note delivered whole was reported as partial the moment it held a dead link."""
    _bind(tmp_path / "v", monkeypatch)
    _write("a.md", "2026-01-01", "yoga details: see [[Ghost]] and [[b]]")
    _write("b.md", "2026-01-01", "the b note about yoga")
    _index()
    from silica.tools.graph import silica_recall

    out = silica_recall(query="yoga details", k=5)
    assert "[[Ghost]] (no such note)" in out["context"]
    assert "a" not in out["partial"], out["partial"]


def test_missing_link_mark_leaves_code_alone(monkeypatch):
    """ADR-0082: a mermaid node `A[[Subroutine]]` is not a wikilink, and the
    mark appended to it broke the fence for mermaid's parser."""
    from silica.kernel.recall import perception as P
    monkeypatch.setattr(P, "_resolver", lambda: (lambda name: name == "Vera"))
    text = ("Vedi [[Vera]] e [[Assente]].\n\n"
            "```mermaid\nflowchart LR\n  A[[Simboli logici]] --> B[Quantificatori]\n```\n"
            "Inline `[[Assente]]` resta codice.")
    out = P.declare_missing_links(text)
    assert "[[Assente]] (no such note)." in out
    assert "A[[Simboli logici]] --> B" in out and "`[[Assente]]` resta" in out
    assert out.count("(no such note)") == 1
