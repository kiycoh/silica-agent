from silica.kernel.lexical import LexicalStore


def test_upsert_rank_and_remove():
    s = LexicalStore()
    s.upsert("notes/apollo", "Apollo 11", "The Apollo 11 moon landing in 1969.")
    s.upsert("notes/cooking", "Risotto", "Stir the rice slowly with stock.")
    ranked = s.rank("apollo moon landing", k=5)
    assert ranked[0][0] == "notes/apollo"           # rare-token query hits
    s.remove("notes/apollo")
    assert all(p != "notes/apollo" for p, _ in s.rank("apollo", k=5))


def test_empty_index_abstains():
    assert LexicalStore().rank("anything") == []    # abstain -> RRF fuses fewer legs


def test_rare_proper_noun_beats_common_words():
    s = LexicalStore()
    s.upsert("n/a", "Zbigniew", "Zbigniew attended the meeting.")
    s.upsert("n/b", "Meeting notes", "The meeting was about the meeting agenda meeting.")
    ranked = s.rank("Zbigniew", k=5)
    assert ranked[0][0] == "n/a"                    # proper noun, BM25 idf lift


def test_fuzzy_title_match_survives_typo():
    s = LexicalStore()
    s.upsert("n/a", "Kubernetes", "container orchestration platform")
    ranked = s.rank("kubernets", k=5)               # one-char typo on the title
    assert ranked and ranked[0][0] == "n/a"


def test_save_load_roundtrip(tmp_path):
    """save() persists and load() reconstitutes an equivalent, queryable index."""
    idx = tmp_path / "lexical.json"
    s = LexicalStore(idx)
    s.upsert("notes/apollo", "Apollo 11", "The Apollo 11 moon landing in 1969.")
    s.upsert("notes/cooking", "Risotto", "Stir the rice slowly with stock.")
    s.save()
    reloaded = LexicalStore.load(idx)
    assert len(reloaded) == 2
    assert reloaded.rank("apollo moon landing", k=5)[0][0] == "notes/apollo"


def test_corrupt_index_quarantines_and_abstains(tmp_path):
    """A corrupt index file loads as an empty, abstaining store (not a crash)."""
    idx = tmp_path / "lexical.json"
    idx.write_bytes(b"{ not valid json ]")
    store = LexicalStore.load(idx)
    assert len(store) == 0
    assert store.rank("anything") == []
