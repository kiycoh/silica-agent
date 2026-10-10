# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The six signal->action seams wired by the 2026-07-26 coherence audit.

Each test pins the JUNCTION, not the metric: the metrics were already computed
and already correct, they just landed nowhere. So every test asserts that a
signal reaches a consumer, and that it stops where it must (nothing destructive
gets auto-tiered, no embedder-free score is compared against a cosine tau).
"""
from __future__ import annotations

from silica.kernel.write import contested
from silica.kernel.analyst_plan import build_task_plan
from silica.kernel.report.graph_report.models import (
    ClusterStat,
    MissingHub,
    StaleLink,
    VaultReport,
)


def _report(**over) -> VaultReport:
    base = dict(
        generated_at="2026-07-26T00:00:00Z", scope="", totals={},
        god_nodes=[], bridges=[], orphans=[], dangling=[], clusters=[],
    )
    base.update(over)
    return VaultReport(**base)


# --- 3 + 4: STALE and MISSING HUB reach the plan, as escalations -------------

def test_stale_links_escalate_with_their_own_options():
    plan = build_task_plan(_report(stale_links=[StaleLink(source="a.md", target="b.md")]))

    card = next(c for c in plan.escalate if "share no concept" in c.reason)
    # Removing a human's wikilink is destructive: it must never be auto/propose.
    assert card.capability_name == ""
    assert not any("share no concept" in c.reason for c in plan.auto + plan.propose)
    # The generic dangling-link options (create/rename) are meaningless here.
    labels = {o["label"] for o in card.options}
    assert labels == {"keep_link", "remove_link", "enrich_source"}


def test_missing_hub_escalates_as_a_creation_choice():
    plan = build_task_plan(_report(missing_hubs=[MissingHub(concept="Bayes", centrality=42.0)]))

    card = next(c for c in plan.escalate if "Bayes" in c.reason)
    assert card.capability_name == ""
    assert "create_hub" in {o["label"] for o in card.options}


def test_escalate_options_default_to_none_for_existing_rules():
    """Pre-existing escalate rules keep the caller's generic options."""
    plan = build_task_plan(_report(dangling=[{"target": "Ghost", "refs": 99}]))
    assert next(c for c in plan.escalate if "Ghost" in c.reason).options is None


# --- 5: the temporal layer is readable --------------------------------------

def test_parse_stamps_reads_every_claim_not_just_the_first():
    body = (
        "<!-- silica: valid_from=2023-05-08 run=aaa -->\nclaim one\n"
        "<!-- silica: valid_from=2021-01-02 run=bbb -->\nclaim two\n"
    )
    stamps = contested.parse_stamps(body)
    assert [s["valid_from"] for s in stamps] == ["2023-05-08", "2021-01-02"]
    # parse_stamp stays the single-claim accessor its existing callers expect.
    assert contested.parse_stamp(body)["run"] == "aaa"
    assert contested.parse_stamps("no stamps here") == []


def test_temporal_stat_is_populated_from_the_analytics_body_scan(tmp_path, monkeypatch):
    from silica.kernel.report.graph_report import compute as compute_mod

    human = "# Plain note\nno frontmatter, so the agent never claimed it\n"
    distilled = (
        "---\nAI: true\nsuperseded_by: '[[Winner]]'\n---\n"
        "<!-- silica: valid_from=2020-03-01 -->\nbody\n\n## Superseded\nold claim\n"
    )
    bodies = {"a.md": human, "b.md": distilled}

    class _Note:
        def __init__(self, content): self.content = content

    class _Driver:
        def read_note(self, nid): return _Note(bodies[nid])

    monkeypatch.setattr("silica.driver.DRIVER", _Driver(), raising=False)

    nodes = [{"id": "a.md", "label": "a", "group": 0}, {"id": "b.md", "label": "b", "group": 0}]
    report = compute_mod.compute_report(
        analytics=True, _nodes_edges_override=(nodes, []), _mtimes_override={},
    )

    tp = report.temporal
    assert tp is not None and tp.notes_scanned == 2
    assert tp.by_tier[contested.TIER_HUMAN] == 1      # a.md: no frontmatter
    assert tp.by_tier[contested.TIER_DISTILLED] == 1  # b.md: AI, no ## Sources
    assert tp.superseded_sections == 1
    assert tp.superseded_notes == 1
    assert tp.stamped == 1
    assert tp.oldest_valid_from == "2020-03-01"
    assert report.totals["superseded_notes"] == 1


def test_temporal_stat_absent_without_analytics():
    """The cheap nucleate path must not gain a per-note body read."""
    nodes = [{"id": "a.md", "label": "a", "group": 0}]
    assert compute_no_analytics(nodes).temporal is None


def compute_no_analytics(nodes):
    from silica.kernel.report.graph_report import compute as compute_mod
    return compute_mod.compute_report(analytics=False, _nodes_edges_override=(nodes, []))


# --- 2: E(vault) delta is attributable per term -----------------------------

def test_energy_persists_its_decomposition(tmp_path, monkeypatch):
    import orjson
    from silica.kernel.report.graph_report.render import write_report

    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(tmp_path), raising=False)
    energy_file = tmp_path / ".silica" / "energy.json"

    r1 = _report(clusters=[ClusterStat(0, 3, "a.md", ["a.md", "b.md", "c.md"], 0.5)])
    write_report(r1, str(tmp_path / "GRAPH_REPORT.md"))
    first = orjson.loads(energy_file.read_bytes())
    assert set(first["terms"]) == {
        "cohesion", "orphans", "dangling", "gaps", "deficits", "contested"
    }
    # The contributions sum to the total — that is what makes them attributable.
    assert sum(first["terms"].values()) == first["value"]
    assert "prev_terms" not in first  # nothing to compare against yet

    # A second report with two orphans: the delta must be attributable to ONE term.
    r2 = _report(
        clusters=r1.clusters,
        orphans=["x.md", "y.md"],
    )
    write_report(r2, str(tmp_path / "GRAPH_REPORT.md"))
    second = orjson.loads(energy_file.read_bytes())
    assert second["prev"] == first["value"]
    moved = {
        t: round(v - second["prev_terms"][t], 4)
        for t, v in second["terms"].items()
        if abs(v - second["prev_terms"][t]) > 1e-9
    }
    assert moved == {"orphans": 2.0}


# --- 7: dedup degrades to the embedder-free leg instead of reporting zero ----

def test_duplicate_pairs_fall_back_to_minhash_without_an_embedder(monkeypatch):
    from silica.kernel.report.graph_report import embed_signals

    twin = "Support Vector Data Description is a one-class boundary method.\n"
    bodies = {
        "One-Class SVDD.md": twin,
        "One-Class Support Vector Data Description.md": twin,
        "Cooking.md": "Risotto needs stock added one ladle at a time.\n",
    }

    class _Note:
        def __init__(self, content): self.content = content

    class _Driver:
        def read_note(self, nid): return _Note(bodies[nid])

    class _EmptyStore:
        def __len__(self): return 0

    monkeypatch.setattr("silica.driver.DRIVER", _Driver(), raising=False)
    monkeypatch.setattr("silica.kernel.recall.embed.get_store", lambda: _EmptyStore())

    report = _report(pagerank_map={k: 0.0 for k in bodies})
    borderline, confirmed = embed_signals._compute_duplicate_pairs(report)

    assert [(d.source, d.target) for d in borderline] == [
        ("One-Class SVDD.md", "One-Class Support Vector Data Description.md")
    ]
    # MinHash Jaccard is not on the cosine scale, so it must never auto-merge:
    # everything goes to the judged band.
    assert confirmed == []

    # Same leg when the store cannot even be built (the other entry into the
    # fallback): an exception must not read as "the vault is clean" either.
    def _boom():
        raise RuntimeError("no index")

    monkeypatch.setattr("silica.kernel.recall.embed.get_store", _boom)
    borderline_2, confirmed_2 = embed_signals._compute_duplicate_pairs(report)
    assert (borderline_2, confirmed_2) == (borderline, confirmed)
