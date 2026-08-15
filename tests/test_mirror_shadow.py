# SPDX-License-Identifier: AGPL-3.0-or-later

"""Safe mode stages `silica/X` as the pending update of `X`: one note, two
paths. Retrieval listed both — /find showed the same lecture note at positions
3 and 4 (observed 2026-08-15). The mirror copy wins; the shadowed original is
dropped from the pool."""
from __future__ import annotations

from silica.kernel.recall.relatedness import RelatedNote, dedupe_mirror_shadows
from silica.kernel.vault_manifest import reset_manifest_cache


def _activate(tmp_path, monkeypatch, manifest: str):
    from silica.config import CONFIG

    if manifest:
        (tmp_path / "vault.yaml").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    reset_manifest_cache()


def _note(path: str, score: float, origin: str = "vault") -> RelatedNote:
    return RelatedNote(path=path, name=path.rsplit("/", 1)[-1], score=score,
                       evidence=[], origin=origin)


def test_mirror_copy_shadows_its_original(tmp_path, monkeypatch):
    _activate(tmp_path, monkeypatch, "write_dir: silica\n")
    pool = [
        _note("silica/appunti/reti lez 3", 0.3),
        _note("appunti/reti lez 3", 0.2),
        _note("appunti/other", 0.1),
    ]
    out = dedupe_mirror_shadows(pool)
    assert [r.path for r in out] == ["silica/appunti/reti lez 3", "appunti/other"]


def test_original_survives_without_a_mirror_twin_in_the_pool(tmp_path, monkeypatch):
    _activate(tmp_path, monkeypatch, "write_dir: silica\n")
    pool = [_note("appunti/reti lez 3", 0.2), _note("silica/appunti/altro", 0.1)]
    assert dedupe_mirror_shadows(pool) == pool


def test_dedupe_is_inert_outside_mirror_mode(tmp_path, monkeypatch):
    """`docs/silica` is Silica's own folder in a repo, not a mirror of it —
    the mirror rules never apply there (adopt.SAFE_WRITE_DIR is the switch)."""
    _activate(tmp_path, monkeypatch, "write_dir: docs/silica\n")
    pool = [_note("docs/silica/X", 0.2), _note("X", 0.1)]
    assert dedupe_mirror_shadows(pool) == pool


def test_memory_lane_results_are_never_touched(tmp_path, monkeypatch):
    """A memory result's path is relative to the MEMORY vault (ADR-0019) —
    comparing it against the active vault's mirror is a cross-vault bug."""
    _activate(tmp_path, monkeypatch, "write_dir: silica\n")
    pool = [_note("silica/X", 0.2), _note("X", 0.1, origin="memory")]
    assert dedupe_mirror_shadows(pool) == pool


def test_fusion_applies_the_dedupe(tmp_path, monkeypatch):
    """The seam is _fuse: every consumer (search, recall, related, autolink)
    goes through it, so the twin never reaches any of them."""
    _activate(tmp_path, monkeypatch, "write_dir: silica\n")
    from silica.kernel.recall.relatedness import _fuse

    embed_rank = [
        ("silica/appunti/reti lez 3", "reti lez 3", 0.9),
        ("appunti/reti lez 3", "reti lez 3", 0.8),
        ("appunti/other", "other", 0.5),
    ]
    out = _fuse(embed_rank, None, k=3)
    paths = [r.path for r in out]
    assert "appunti/reti lez 3" not in paths
    assert "silica/appunti/reti lez 3" in paths
