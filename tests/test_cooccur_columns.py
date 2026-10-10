"""The co-occurrence store on disk as columns (ADR-0073): a reload answers every
accessor exactly as the dicts did, a version-1 file is rewritten once, and the
vocabulary keeps only what some row uses."""
import io

import numpy as np
import orjson
import pytest

from silica.kernel.recall import cooccurrence as cooc
from silica.kernel.recall.cooccurrence import CooccurStore, build_contribution

NOTES = {
    "a/one": ("One", "Quick sort splits the array. The pivot sorts the array again."),
    "a/two": ("Two", "A heap sort builds a heap from the array and sorts it."),
    "b/three": ("Three", "Bread needs flour, water and yeast; the yeast makes bread rise."),
}


def _store(path):
    s = CooccurStore(path=path)
    for p, (name, body) in NOTES.items():
        s.upsert_note(p, build_contribution(name, body))
    return s


def _answers(s):
    return {
        "paths": s.paths(),
        "nodes": {p: list(s.note_nodes(p).items()) for p in s.paths()},
        "agg": [(k, list(v.items())) for k, v in s._aggregate()[0].items()],
        "labels": list(s._aggregate()[1].items()),
        "scoped": s._aggregate(scope="a")[0],
        "postings": s.stem_postings(),
        "lengths": s.doc_lengths(),
        "note_adj": s.note_adjacency(),
        "neighbors": s.neighbors("array"),
    }


def test_a_reload_answers_every_accessor_as_the_dicts_did_in_the_same_order(tmp_path):
    p = tmp_path / "cooccurrence.json"
    written = _store(p)
    before = _answers(written)
    written.save()
    assert p.read_bytes()[:2] == b"PK"
    reloaded = CooccurStore(path=p)
    assert all(isinstance(c, cooc._Cols) for c in reloaded._notes.values())
    assert _answers(reloaded) == before


def test_a_version_1_file_is_rewritten_once_on_load(tmp_path):
    p = tmp_path / "cooccurrence.json"
    s = _store(tmp_path / "scratch.json")
    p.write_bytes(orjson.dumps({"version": 1, "lang": "english", "notes": dict(s._notes)}))
    migrated = CooccurStore(path=p)
    assert p.read_bytes()[:2] == b"PK"
    assert _answers(migrated) == _answers(s) == _answers(CooccurStore(path=p))


def test_a_view_and_a_new_dict_save_together(tmp_path):
    """An upsert after a load keeps the dict until the next load: no column copy."""
    p = tmp_path / "cooccurrence.json"
    _store(p).save()
    s = CooccurStore(path=p)
    s.upsert_note("c/four", build_contribution("Four", "Yeast and flour again, with salt."))
    assert isinstance(s._notes["c/four"], dict)
    expected = _answers(s)
    s.save()
    assert _answers(CooccurStore(path=p)) == expected


def test_a_deleted_notes_words_leave_the_vocabulary(tmp_path):
    p = tmp_path / "cooccurrence.json"
    _store(p).save()
    s = CooccurStore(path=p)
    s.delete_note("b/three")
    s.save()
    with np.load(io.BytesIO(p.read_bytes()), allow_pickle=False) as z:
        vocab = orjson.loads(z["meta"].tobytes())["vocab"]
    assert "yeast" not in {w.lower() for w in vocab}
    assert "array" in {w.lower() for w in vocab}


def test_a_fractional_weight_is_refused_not_truncated(tmp_path):
    s = CooccurStore(path=tmp_path / "cooccurrence.json")
    s.upsert_note("x", {"nodes": {"a": {"label": "a", "count": 1}, "b": {"label": "b", "count": 1}},
                        "edges": [["a", "b", 1.5]]})
    with pytest.raises(ValueError, match="whole numbers"):
        s.save()


def test_frozen_lang_reads_the_column_header(tmp_path, monkeypatch):
    p = tmp_path / "cooccurrence.json"
    s = _store(p)
    s.lang = "italian"
    s.save()
    monkeypatch.setattr(cooc, "_index_path_for", lambda vault: p)
    assert cooc.frozen_lang("any") == "italian"
