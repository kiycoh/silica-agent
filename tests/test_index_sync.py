"""Invocation-time index sweep (kernel/recall/sync.py).

Covers the out-of-band half of index freshness: notes created, edited, or
deleted while no Silica process was running (Obsidian, rm, git checkout).
The write path's own freshness hooks are covered in test_deferred_flush.py.

The reconcile tests migrated here from test_deferred_flush.py when the
startup reconcile was absorbed into the sweep (same ADD/PRUNE semantics,
plus MODIFY detection via mtime stamps + content-signature backstop).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from silica.driver.base import NoteRef
from silica.kernel.recall import cooccurrence as cooc
from silica.kernel.recall import sync


class _Emb:
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float(len(t) % 5), 1.0, 0.0] for t in texts]


class _DownEmb(_Emb):
    def embed(self, texts):
        raise RuntimeError("embedder down")


@pytest.fixture
def enabled(monkeypatch):
    """Re-enable the sweep (conftest disables it suite-wide) and silence the
    debounce so every sweep() call in a test really sweeps."""
    monkeypatch.setattr("silica.config.CONFIG.index_sweep", True)
    monkeypatch.setattr(sync, "_MIN_INTERVAL", 0.0)


def _driver(files: dict[str, str], mtimes: dict[str, float] | None = None):
    """Stub driver over {idx_path: body}. mtimes keyed by idx_path, default 1.0."""
    mt = mtimes or {}
    return SimpleNamespace(
        list_files=lambda folder="": [
            NoteRef(p.rsplit("/", 1)[-1], p + ".md") for p in files
        ],
        read_note=lambda ref: SimpleNamespace(
            content=files[str(ref).removesuffix(".md")]
        ),
        mtime_of=lambda ref: mt.get(
            (ref.path if hasattr(ref, "path") else str(ref)).removesuffix(".md"), 1.0
        ),
    )


def _seed_embed(embed, ei, files: dict[str, str], embedder=None):
    """Seed the embed index through build_index so entries carry real content
    signatures (an unchanged note is one whose stored hash matches its text)."""
    embed.clear()
    from silica.kernel.recall.embed import build_index
    build_index(embedder or _Emb(),
                [(p, p.rsplit("/", 1)[-1], b) for p, b in files.items()],
                store=embed.get_store())


# --- gates -----------------------------------------------------------------

def test_sweep_respects_config_gate(monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.index_sweep", False)
    monkeypatch.setattr(
        "silica.driver.DRIVER",
        SimpleNamespace(list_files=lambda folder="": pytest.fail("gated sweep must not scan")),
    )
    assert sync.sweep(force=True).skipped


def test_sweep_debounces(monkeypatch, enabled):
    monkeypatch.setattr(sync, "_MIN_INTERVAL", 3600.0)
    monkeypatch.setattr("silica.driver.DRIVER", _driver({}))
    first = sync.sweep()          # cold stores → skipped, but debounce armed
    assert first.skipped
    called = []
    monkeypatch.setattr("silica.driver.DRIVER",
                        SimpleNamespace(list_files=lambda folder="": called.append(1)))
    assert sync.sweep().skipped   # within the interval → no scan
    assert not called


def test_sweep_skips_cold_indexes(tmp_path, monkeypatch, enabled):
    """Every store cold → the sweep must not even enumerate the vault
    (explicit /embed, /cooccur, /lexical own the first build)."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    embed.clear()

    def _boom(*a, **k):
        raise AssertionError("sweep must not enumerate on cold indexes")

    monkeypatch.setattr("silica.driver.DRIVER",
                        SimpleNamespace(list_files=_boom, mtime_of=lambda r: 1.0))
    assert sync.sweep(force=True).skipped


def test_sweep_abstains_without_mtime(tmp_path, monkeypatch, enabled):
    """A driver without mtime_of (ws backend) has no change signal — abstain."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    _seed_embed(embed, None, {"a": "body a"})
    monkeypatch.setattr("silica.driver.DRIVER",
                        SimpleNamespace(list_files=lambda folder="": []))
    assert sync.sweep(force=True).skipped


# --- ADD (migrated from the startup reconcile) -----------------------------

def test_sweep_embeds_missing_note(tmp_path, monkeypatch, enabled):
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    files = {"a": "body a", "b": "body b"}
    _seed_embed(embed, None, files)
    files["c"] = "a body about c"
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())

    stats = sync.sweep(force=True)
    assert stats.embedded == 1          # only c — a, b hash-match and stamp only
    assert embed.get_store().has("c")
    assert stats.stamped == 3


def test_sweep_skips_embeds_past_cap(tmp_path, monkeypatch, enabled):
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    _seed_embed(embed, None, {"a": "body a"})
    monkeypatch.setattr(sync, "_RECONCILE_CAP", 2)

    files = {"a": "body a"} | {f"n{i}": f"body {i}" for i in range(5)}
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    embedder = _DownEmb()  # embed() raising would fail the test if reached
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: embedder)

    stats = sync.sweep(force=True)
    assert stats.embedded == 0          # 5 changed > cap 2 → defer to /embed
    # changed notes stay unstamped so they are retried once /embed catches up
    assert not any(p in sync._load_stamps() for p in (f"n{i}" for i in range(5)))


# --- MODIFY (the new leg) --------------------------------------------------

def test_sweep_reembeds_out_of_band_edit(tmp_path, monkeypatch, enabled):
    """A note edited by hand (mtime moved, content changed) is re-embedded and
    re-contributed; its stamp advances."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    files = {"a": "body a", "b": "body b"}
    _seed_embed(embed, None, files)
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())
    monkeypatch.setattr("silica.driver.DRIVER", _driver(dict(files)))
    sync.sweep(force=True)              # baseline: everything stamped at mtime 1.0

    old_hash = embed.get_store().get_content_hash("a")
    files["a"] = "a completely different body"
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files, mtimes={"a": 2.0}))
    stats = sync.sweep(force=True)

    assert stats.embedded == 1
    assert embed.get_store().get_content_hash("a") != old_hash
    assert sync._load_stamps()["a"] == 2.0


def test_sweep_touch_only_stamps_without_reembed(tmp_path, monkeypatch, enabled):
    """mtime moved but content identical (touch, git checkout of same bytes):
    stamp advances, the embedder is never called."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    files = {"a": "body a"}
    _seed_embed(embed, None, files)
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    sync.sweep(force=True)

    embedder = _Emb()
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: embedder)
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files, mtimes={"a": 2.0}))
    stats = sync.sweep(force=True)

    assert stats.embedded == 0
    assert embedder.calls == 0
    assert sync._load_stamps()["a"] == 2.0


def test_sweep_embedder_down_leaves_changed_unstamped(tmp_path, monkeypatch, enabled):
    """Embed leg fails → the changed note is NOT stamped (retried next sweep),
    but the deterministic cooccur leg still refreshes it."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    files = {"a": "neural network training gradient descent"}
    _seed_embed(embed, None, files)
    cstore = cooc.get_cooccur_store(lang="english")
    cstore.upsert_note("a", {})
    cstore.save()

    # Establish the stamp baseline with a working embedder first, so the edit
    # below is a real change rather than an unstamped first sighting.
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())
    monkeypatch.setattr("silica.driver.DRIVER", _driver(dict(files)))
    sync.sweep(force=True)
    assert sync._load_stamps()["a"] == 1.0

    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _DownEmb())
    files["a"] = "sailing boat harbour regatta wind"
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files, mtimes={"a": 2.0}))
    stats = sync.sweep(force=True)

    assert stats.embedded == 0
    assert sync._load_stamps()["a"] == 1.0  # stamp NOT advanced → retry next sweep
    assert stats.refreshed >= 1             # cooccur refreshed anyway


# --- cooccur ADD (the leg the old reconcile never had) ---------------------

def test_sweep_adds_new_note_to_cooccur(tmp_path, monkeypatch, enabled):
    """The ADD leg the old reconcile lacked: a note created out-of-band enters
    the co-occurrence graph. On this first sweep 'a' is already in the store,
    so it is baseline (not refreshed) — only the genuinely new 'b' costs work."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    embed.clear()   # embed cold — embedder-free vault
    cstore = cooc.get_cooccur_store(lang="english")
    cstore.upsert_note("a", {})
    cstore.save()

    files = {"a": "old note body", "b": "neural network training gradient"}
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    stats = sync.sweep(force=True)

    assert "b" in set(cooc.get_cooccur_store().paths())
    assert stats.refreshed == 1   # only 'b'; 'a' is first-sweep baseline


def test_first_sweep_does_not_rebuild_known_notes(tmp_path, monkeypatch, enabled):
    """Upgrade path: a vault whose cooccur index predates stamping must not
    re-contribute every note on the first query (3.5s at 758 notes)."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    embed.clear()
    cstore = cooc.get_cooccur_store(lang="english")
    files = {f"n{i}": f"body {i} neural network" for i in range(30)}
    for p in files:
        cstore.upsert_note(p, {})
    cstore.save()

    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    stats = sync.sweep(force=True)
    assert stats.refreshed == 0       # all known → stamped only
    assert stats.stamped == 30

    # ...but a real edit AFTER the baseline still refreshes.
    files["n3"] = "sailing boat harbour regatta"
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files, mtimes={"n3": 2.0}))
    assert sync.sweep(force=True).refreshed == 1


# --- PRUNE (migrated from the startup reconcile) ---------------------------

def test_sweep_prunes_out_of_band_deletion(tmp_path, monkeypatch, enabled):
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    files = {"a": "body a", "b": "body b", "c": "body c"}
    _seed_embed(embed, None, files)
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())

    del files["c"]  # deleted out-of-band
    monkeypatch.setattr("silica.driver.DRIVER", _driver(files))
    stats = sync.sweep(force=True)

    store = embed.get_store()
    assert store.has("a") and store.has("b")
    assert not store.has("c")
    assert stats.pruned == 1


def test_sweep_refuses_prune_on_empty_live_view(tmp_path, monkeypatch, enabled):
    """Empty vault view against a populated index = misconfig, not deletion."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    _seed_embed(embed, None, {"a": "x", "b": "y", "c": "z"})

    monkeypatch.setattr("silica.driver.DRIVER", _driver({}))
    stats = sync.sweep(force=True)
    assert stats.pruned == 0
    assert len(embed.get_store()) == 3


def test_sweep_refuses_prune_when_view_half_missing(tmp_path, monkeypatch, enabled):
    """A view missing more than half a populated store smells like a partial
    fs read — ratio guard keeps every entry."""
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    _seed_embed(embed, None, {f"n{i}": f"body {i}" for i in range(100)})
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())

    live = {f"n{i}": f"body {i}" for i in range(40)}  # 60/100 absent
    monkeypatch.setattr("silica.driver.DRIVER", _driver(live))
    stats = sync.sweep(force=True)
    assert stats.pruned == 0
    assert len(embed.get_store()) == 100


def test_sweep_prunes_cooccur_orphans(tmp_path, monkeypatch, enabled):
    import silica.kernel.recall.embed as embed
    monkeypatch.setattr(embed, "_index_path", lambda: tmp_path / "embeddings.json")
    embed.clear()
    store = cooc.get_cooccur_store(lang="english")
    for p in ("a", "b", "c"):
        store.upsert_note(p, {})
    store.save()

    monkeypatch.setattr("silica.driver.DRIVER", _driver({"a": "x", "b": "y"}))
    stats = sync.sweep(force=True)
    assert set(cooc.get_cooccur_store().paths()) == {"a", "b"}
    assert stats.pruned == 1


# --- wiring ----------------------------------------------------------------

def test_facade_retrieve_sweeps_first(monkeypatch):
    from silica.kernel.recall.perception import facade_retrieve
    called = []
    monkeypatch.setattr(sync, "sweep", lambda **kw: called.append(1))
    # Empty stores → facade returns (None, None), but the sweep ran first.
    facade_retrieve("q", k=3, use_embedder=False)
    assert called == [1]
