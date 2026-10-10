# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0062 Decisions 5 to 7: a daily note in the graph.

It is typed `daily`, related to other notes by its blocks one at a time, and
kept out of the whole-note k-NN, where its template made it a neighbour of
every other day (measured on the founder's vault: 4 of 4).
"""
from __future__ import annotations

import json
import math

import pytest

from silica.kernel.recall import daily_layer, embed, graph_export

DAILY = """---
tags:
---
## Da fare oggi
- [ ] Esame Machine Learning ⏫ 📅 2026-11-10
\t- [ ] Progetto pratico ⏫
- [ ]

A paragraph about the
Datapolis presentation.

```tasks
not done
path includes daily/
```
## In programma
"""


def test_blocks_are_items_and_paragraphs_without_the_template():
    assert daily_layer.blocks(DAILY) == [
        "Esame Machine Learning",
        "Progetto pratico",
        "A paragraph about the Datapolis presentation.",
    ]


class _Store:
    """The two EmbedStore calls the layer makes, over exact vectors."""

    def __init__(self, vecs):
        self._vecs = vecs

    def __len__(self):
        return len(self._vecs)

    def get_vec(self, key):
        return self._vecs.get(key)

    def cosine_top_k(self, vec, k=5, exclude=None):
        def cos(a, b):
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(x * x for x in b)) or 1.0
            return sum(x * y for x, y in zip(a, b)) / (na * nb)
        ranked = sorted(((cos(vec, v), p) for p, v in self._vecs.items()
                         if p not in (exclude or set())), reverse=True)
        return [{"path": p, "name": p, "score": round(s, 4)} for s, p in ranked[:k]]

    def cosine_top_k_batch(self, keys, k=5, *, exclude_self=True, block=256):
        return {key: self.cosine_top_k(self._vecs[key], k, {key}) for key in keys
                if key in self._vecs}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    obs = tmp_path / ".obsidian"
    obs.mkdir()
    (obs / "core-plugins.json").write_text(json.dumps({"daily-notes": True}), encoding="utf-8")
    (obs / "daily-notes.json").write_text(json.dumps({"folder": "daily"}), encoding="utf-8")
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-09-18.md").write_text(DAILY, encoding="utf-8")
    (tmp_path / "daily" / "2026-09-19.md").write_text("- [ ] Esame Machine Learning\n",
                                                      encoding="utf-8")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(tmp_path))
    return tmp_path


def _nodes():
    return [{"id": p, "type": "note"} for p in
            ("daily/2026-09-18.md", "daily/2026-09-19.md", "ML.md", "Datapolis.md", "Cooking.md")]


def test_stamp_types_the_daily_notes_and_dates_them(vault):
    nodes = _nodes()
    daily_layer.stamp_daily(nodes, vault)
    got = {n["id"]: (n["type"], n.get("daily")) for n in nodes}
    assert got["daily/2026-09-18.md"] == ("daily", "2026-09-18")
    assert got["ML.md"] == ("note", None)


def _stores(monkeypatch, *, blocks: dict[str, list[float]]):
    notes = _Store({
        # The whole-note vectors of the two days are the template's: identical.
        "daily/2026-09-18": [1.0, 1.0, 1.0], "daily/2026-09-19": [1.0, 1.0, 1.0],
        "ML": [1.0, 0.0, 0.0], "Datapolis": [0.0, 1.0, 0.0], "Cooking": [0.0, 0.0, 1.0],
    })
    bstore = _Store({daily_layer.block_key(t): v for t, v in blocks.items()})
    monkeypatch.setattr(embed, "get_store", lambda: notes)
    monkeypatch.setattr(daily_layer, "block_store", lambda: bstore)


def test_a_day_is_related_by_its_blocks_one_edge_per_neighbour(vault, monkeypatch):
    _stores(monkeypatch, blocks={
        "Esame Machine Learning": [0.9, 0.1, 0.0],
        "Progetto pratico": [0.7, 0.2, 0.0],          # ML again, weaker
        "A paragraph about the Datapolis presentation.": [0.1, 0.9, 0.0],
    })
    nodes = _nodes()
    daily_layer.stamp_daily(nodes, vault)
    edges = graph_export.knn_edges(nodes, k=1)
    day = sorted((e["from"], e["to"]) for e in edges
                 if "daily/2026-09-18.md" in (e["from"], e["to"]))
    assert day == [("daily/2026-09-18.md", "Datapolis.md"), ("daily/2026-09-18.md", "ML.md")]
    ml = next(e for e in edges if e["to"] == "ML.md" and e["from"] == "daily/2026-09-18.md")
    assert ml["type"] == "SIMILAR" and ml["score"] == pytest.approx(0.9939, abs=1e-3)
    # The second day's one block is the same text, so the same vector.
    assert ("daily/2026-09-19.md", "ML.md") in {(e["from"], e["to"]) for e in edges}
    # Never day to day, though their whole-note vectors are identical.
    assert not [e for e in edges if e["from"].startswith("daily/") and e["to"].startswith("daily/")]


def test_whole_note_knn_never_offers_a_day(vault, monkeypatch):
    _stores(monkeypatch, blocks={})
    nodes = _nodes()
    daily_layer.stamp_daily(nodes, vault)
    edges = graph_export.knn_edges(nodes, k=4)
    assert edges and not [e for e in edges if "daily/" in e["from"] + e["to"]]


def test_a_block_vector_of_another_dimension_draws_nothing(vault, monkeypatch):
    # After a model swap the old block vectors cannot be compared; a 0.0
    # score must not become an edge to an arbitrary note.
    notes = _Store({"ML": [1.0, 0.0, 0.0]})
    monkeypatch.setattr(embed, "get_store", lambda: notes)
    monkeypatch.setattr(daily_layer, "block_store", lambda: _Store(
        {daily_layer.block_key("Esame Machine Learning"): [0.0, 0.0, 0.0]}))
    nodes = [{"id": "daily/2026-09-19.md", "type": "note"}, {"id": "ML.md", "type": "note"}]
    daily_layer.stamp_daily(nodes, vault)
    assert daily_layer.daily_edges(nodes) == []


class _Embedder:
    model = "fake"

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


def test_refresh_embeds_each_block_once_and_drops_the_stale(vault, tmp_path):
    store = embed.EmbedStore(tmp_path / "blocks.json")
    store.upsert(daily_layer.block_key("gone"), "gone", [1.0, 0.0], content_hash="fake")
    emb = _Embedder()
    assert daily_layer.refresh_blocks(emb, vault, store=store) == 3
    # "Esame Machine Learning" is in both days: embedded once.
    assert sorted(emb.calls[0]) == sorted(["Esame Machine Learning", "Progetto pratico",
                                           "A paragraph about the Datapolis presentation."])
    assert store.get_vec(daily_layer.block_key("gone")) is None
    emb2 = _Embedder()
    assert daily_layer.refresh_blocks(emb2, vault, store=store) == 0 and emb2.calls == []


def test_a_model_swap_re_embeds_the_blocks(vault, tmp_path):
    store = embed.EmbedStore(tmp_path / "blocks.json")
    daily_layer.refresh_blocks(_Embedder(), vault, store=store)
    other = _Embedder()
    other.model = "other"
    assert daily_layer.refresh_blocks(other, vault, store=store) == 3


def test_a_lone_day_is_drawn_neutral(vault):
    nodes = _nodes()
    daily_layer.stamp_daily(nodes, vault)
    for i, n in enumerate(nodes):
        n["group"], n["color"] = i, {"background": f"#00000{i}"}
    nodes[1]["group"] = nodes[2]["group"]   # the second day sits in ML's community
    daily_layer.neutral_lone_dailies(nodes)
    assert nodes[0]["color"] == graph_export._NODE_DEFAULT_COLOR
    assert nodes[1]["color"] == {"background": "#000001"}


def test_the_export_draws_a_lone_day_neutral(monkeypatch, tmp_path):
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    monkeypatch.setattr(graph_export, "build_graph_data", lambda folder="": (
        [{"id": "daily/2026-09-18.md", "label": "2026-09-18", "type": "daily",
          "daily": "2026-09-18", "path": "daily/2026-09-18.md", "size": 16},
         {"id": "a.md", "label": "a", "type": "note", "path": "a.md", "size": 16},
         {"id": "b.md", "label": "b", "type": "note", "path": "b.md", "size": 16}],
        [{"id": "e0", "from": "a.md", "to": "b.md", "type": "EXTRACTED"}],
    ))
    monkeypatch.setattr(graph_export, "knn_edges", lambda nodes, k=6: [])
    seen = {}
    real_render = gv.render_html

    def spy(nodes, *a, **kw):
        seen["nodes"] = {n["id"]: n for n in nodes}
        return real_render(nodes, *a, **kw)

    monkeypatch.setattr(gv, "render_html", spy)
    gv.export_graph(output_path=str(tmp_path / "g.html"))
    assert seen["nodes"]["daily/2026-09-18.md"]["color"] == graph_export._NODE_DEFAULT_COLOR
    assert seen["nodes"]["a.md"]["color"] != graph_export._NODE_DEFAULT_COLOR


def test_the_graph_export_tool_refreshes_the_blocks_first(monkeypatch, vault):
    import silica.tools.graph as tg
    import silica.ui.web.graph_view as gv

    calls = []
    monkeypatch.setattr(tg, "silica_cooccurrence_refresh", lambda folder="": {})
    monkeypatch.setattr(daily_layer, "refresh_blocks",
                        lambda embedder, v, store=None: calls.append(str(v)) or 0)
    monkeypatch.setattr(gv, "export_graph", lambda **kw: calls.append("export") or {})
    tg.silica_graph_export(output_path="g.html")
    assert calls == [str(vault), "export"]
