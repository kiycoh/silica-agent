# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pins the LexicalStore save/load roundtrip the write choke-point hook
relies on: a committed note becomes queryable after an upsert+save+load
cycle. The hook's index-presence gating and wiring are verified by
inspection of silica/router/states/write.py and orchestrator.py's
_flush_indexes (no lexical.json on disk => the hook never touches the
store, so non-lexical vaults stay byte-identical)."""
from silica.kernel.recall.lexical import LexicalStore


def test_upsert_then_query_roundtrip(tmp_path):
    # The write hook's job reduced to its core: a committed note becomes queryable.
    idx = tmp_path / "lexical.json"
    s = LexicalStore(idx)
    s.upsert("notes/mars", "Mars rover", "Perseverance landed in Jezero crater.")
    s.save()
    reloaded = LexicalStore.load(idx)
    assert reloaded.rank("Jezero", k=3)[0][0] == "notes/mars"


def test_an_index_keyed_with_md_loads_keyed_as_the_sweep_keys_notes(tmp_path):
    """An index built 2026-09-21 kept ".md" in its keys; no key met a live
    path, and a sweep pruned 680 untouched notes as deleted (2026-10-23)."""
    import orjson

    idx = tmp_path / "lexical.json"
    idx.write_bytes(orjson.dumps({
        "docs": {"notes/mars.md": {"jezero": 1}, "notes/venus.md": {"ishtar": 1}, "notes/venus": {"maat": 1}},
        "len": {"notes/mars.md": 1, "notes/venus.md": 1, "notes/venus": 1},
        "name": {"notes/mars.md": "Mars", "notes/venus.md": "Venus", "notes/venus": "Venus"}}))
    s = LexicalStore.load(idx)
    assert sorted(s.paths()) == ["notes/mars", "notes/venus"]
    assert s.rank("Jezero", k=3)[0][0] == "notes/mars"
    assert s.rank("Maat", k=3)[0][0] == "notes/venus" and not s.rank("Ishtar", k=3)   # the current spelling wins
