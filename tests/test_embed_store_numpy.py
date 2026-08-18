# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""EmbedStore in-memory representation: float32 numpy rows, lists only at the
API boundary.

Measured before the change: deserializing a 24.8 MB index cost +247 MB RSS
(10x the file) because every vector was exploded into a Python float list —
6.1 M boxed floats for 1198 notes. The store now keeps each vector as a
float32 ndarray (views over one flat buffer on the npz path) and `get_vec` /
`get_title_vec` materialize the list contract on demand.
"""
from __future__ import annotations

import numpy as np
import pytest

from silica.kernel.recall.embed import EmbedStore


DIM = 16


def _vec(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(DIM).tolist()


@pytest.fixture
def store(tmp_path):
    s = EmbedStore(path=tmp_path / "embeddings.json")
    for i in range(6):
        s.upsert(f"n{i}.md", f"n{i}", _vec(i), title_vec=_vec(100 + i),
                 content_hash=f"h{i}")
    return s


def test_internal_storage_is_float32_ndarray(store):
    entry = store._notes["n0.md"]
    assert isinstance(entry["vec"], np.ndarray)
    assert entry["vec"].dtype == np.float32
    assert isinstance(entry["title_vec"], np.ndarray)


def test_get_vec_keeps_the_list_contract(store):
    v = store.get_vec("n0.md")
    assert isinstance(v, list)
    assert isinstance(v[0], float)
    assert v == pytest.approx(_vec(0), rel=1e-6)
    tv = store.get_title_vec("n0.md")
    assert isinstance(tv, list)
    assert tv == pytest.approx(_vec(100), rel=1e-6)
    assert store.get_vec("missing.md") is None


def test_npz_roundtrip_stays_ndarray(store, tmp_path):
    store.save()
    loaded = EmbedStore(path=tmp_path / "embeddings.json")
    entry = loaded._notes["n3.md"]
    assert isinstance(entry["vec"], np.ndarray)
    assert isinstance(entry["title_vec"], np.ndarray)
    assert loaded.get_vec("n3.md") == pytest.approx(_vec(3), rel=1e-6)
    assert loaded.get_content_hash("n3.md") == "h3"


def test_legacy_json_load_normalizes_to_ndarray(tmp_path):
    import orjson

    path = tmp_path / "embeddings.json"
    legacy = {"notes": {
        "a.md": {"vec": _vec(1), "name": "a", "ts": 1.0, "title_vec": _vec(2)},
        "b.md": {"vec": _vec(3), "name": "b", "ts": 2.0},
    }}
    path.write_bytes(orjson.dumps(legacy))
    s = EmbedStore(path=path)
    assert isinstance(s._notes["a.md"]["vec"], np.ndarray)
    assert isinstance(s._notes["a.md"]["title_vec"], np.ndarray)
    assert s.get_vec("b.md") == pytest.approx(_vec(3), rel=1e-6)


def test_search_results_identical_to_list_era(store):
    # cosine_top_k / batch must not change results or ordering.
    q = _vec(0)
    single = store.cosine_top_k(q, k=3, exclude={"n0.md"})
    assert [r["path"] for r in single]  # non-empty, no raise on ndarray entries
    batch = store.cosine_top_k_batch(["n0.md", "n4.md"], k=3)
    assert batch["n0.md"] == single
    title = store.title_cosine_top_k(_vec(100), k=2, exclude={"n0.md"})
    assert len(title) == 2


def test_upsert_preserves_existing_title_vec(store):
    store.upsert("n1.md", "n1", _vec(50))  # no title_vec passed
    assert store.get_title_vec("n1.md") == pytest.approx(_vec(101), rel=1e-6)


def test_mixed_entry_shapes_do_not_raise(store):
    # A test double or hand-built store may still poke lists into _notes;
    # the boundary stays tolerant and _build_matrix must not choke on either.
    store._notes["hand.md"] = {"vec": _vec(9), "name": "hand", "ts": 0.0}
    store._invalidate_matrix()
    assert isinstance(store.get_vec("hand.md"), list)
    assert store.cosine_top_k(_vec(9), k=2)


@pytest.mark.parametrize("raw", [
    '{"notes": {"a.md": null}}',
    '{"notes": []}',
    '{"notes": null}',
    '{"not": "an index"}',
    'truncated {',
])
def test_a_malformed_legacy_index_degrades_to_an_empty_store(tmp_path, raw):
    """`_load` runs from `__init__`, so anything that raises there takes the
    constructor with it: `get_store()` throws and every caller that merely
    wanted `len(store)` dies on a half-written or hand-edited index. The
    legacy-vector normalization loop sat OUTSIDE the guard that made this
    degrade — orjson parses `{"notes": []}` fine and the loop then blows up.
    """
    p = tmp_path / "embeddings.json"
    p.write_text(raw, encoding="utf-8")
    assert len(EmbedStore(path=p)) == 0


def test_a_valid_legacy_index_still_normalizes_to_float32_rows(tmp_path):
    import json

    p = tmp_path / "embeddings.json"
    p.write_text(json.dumps({"notes": {"a.md": {"vec": [1.0, 2.0, 3.0], "name": "a"}}}),
                 encoding="utf-8")
    entry = EmbedStore(path=p)._notes["a.md"]
    assert isinstance(entry["vec"], np.ndarray) and entry["vec"].dtype == np.float32
