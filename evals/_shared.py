# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Provenance + lever-liveness helpers shared by the benchmark runners.

Provenance answers "which code, which data, which run produced this number":
git SHA (via silica.kernel.code.gitstate), dataset path + sha256, a timestamped run
id. Liveness answers "was the lever I switched on actually live", so an A/B
cannot silently compare baseline vs baseline (empty lexical index, dead
reranker) or trust an unpinned nondeterministic provider route.
"""
from __future__ import annotations

import datetime
import hashlib
import sys
from pathlib import Path


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_id() -> str:
    """A per-run timestamp id (second resolution) — unique enough to keep two
    reruns from clobbering, and to tie a metrics file to its provenance block."""
    return datetime.datetime.now().strftime("%Y%m%dT%H%M%S")


def git_sha() -> str | None:
    """HEAD sha of the repo this code lives in, or None outside a git repo."""
    from silica.kernel.code import gitstate

    root = gitstate.find_repo_root(Path(__file__))
    return gitstate.head_ref(root) if root else None


def provenance(data_path: str | Path, *, rid: str | None = None) -> dict:
    """Attribution block: {run_id, git_sha, dataset:{path, sha256}}. The dataset
    sha256 disambiguates positional question ids (conv-26_q0 names different
    questions across data files — the hash says which file)."""
    p = Path(data_path)
    return {
        "run_id": rid or run_id(),
        "git_sha": git_sha(),
        "dataset": {"path": str(p), "sha256": _sha256_file(p)},
    }


def embedding_model(config, live: bool) -> str | None:
    return getattr(config, "embedding_model", None) if live else None


# --- Frozen-corpus checkpoint ------------------------------------------------
#
# `--reuse-vaults` used to mean "the session directory exists", which adopts a
# corpus distilled under a different prompt or model, or an ingest killed
# halfway, and then reports it as a frozen baseline. The stamp records the
# fields that decided the corpus plus the note count it claims to hold; reuse
# demands an exact match on all of them and re-ingests from zero otherwise.
# No partial resume and no migration on purpose: a corpus that half matches is
# not a corpus, and re-running the ingest is cheaper than a wrong number.

_STAMP_NAME = "corpus.json"


def lens_fingerprint(profile: str | None, distill: bool = True) -> str:
    """Name the distiller lens that produced a corpus.

    Rendered with the per-session placeholders fixed, so the fingerprint moves
    when the prompt template or the profile moves and stays put when only the
    session date does. A verbatim arm never calls the distiller, so it has no
    lens and no prompt edit can invalidate its corpus.
    """
    if not distill:
        return ""
    from silica.kernel import distill_cache, prep_delegation

    return distill_cache.prompt_fingerprint(
        prep_delegation.render_prompt(target="sessions", profile=profile))


def write_corpus_stamp(vault: Path, fields: dict, *, present: int) -> None:
    """Record how this corpus was built, next to the corpus itself."""
    import json

    from silica.kernel.recall.paths import atomic_write_bytes

    vault.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"fields": fields, "notes": present}, indent=2, sort_keys=True)
    atomic_write_bytes(vault / _STAMP_NAME, blob.encode("utf-8"))


def wipe_index_namespace(vault: Path) -> None:
    """Drop ``vault``'s ~/.silica/index/<digest>/ (embeddings, cooccur,
    episodic, deferred bundles) so a from-scratch re-ingest starts clean.
    Rebuilding notes without this leaves the previous corpus's vectors behind
    and the arm measures a blend of the two.

    The vault is an argument and not `index_dir()` on purpose: resolving it
    from the ambient CONFIG makes a destructive rmtree depend on whether the
    caller happened to bind a vault first, and an adapter test that calls a
    loader directly would delete the developer's real index.
    """
    import shutil

    from silica.kernel.recall import paths as kpaths

    shutil.rmtree(kpaths.index_dir_for(str(vault)), ignore_errors=True)


def corpus_reusable(vault: Path, fields: dict, *, present: int) -> bool:
    """True only when the stamp matches this run field for field AND still
    holds the number of notes it claims. The count is the invariant the old
    `is_dir()` check had no way to see: a killed ingest leaves the fields
    right and the notes missing."""
    import json

    try:
        stamp = json.loads((vault / _STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stamp, dict):
        return False
    return stamp.get("fields") == fields and stamp.get("notes") == present


# --- Lever liveness (fail fast, never fake a null A/B) -----------------------

def assert_lexical_live() -> None:
    """--lexical over an empty index is a documented no-op; refuse it rather
    than report a null result that is an artifact. Assumes the vault is bound."""
    from silica.kernel.recall.lexical import get_lexical_store

    if len(get_lexical_store()) == 0:
        raise SystemExit(
            "--lexical set but the lexical index is empty: build it first with "
            "silica_lexical_refresh (the /lexical CLI). Refusing to run a no-op arm.")


def assert_reranker_live(config) -> None:
    """A configured reranker whose server is down silently abstains and fakes
    rerank == embed-only. Probe it once; fail fast if it will not answer. No
    reranker configured is not a lie (config records reranker=None), so pass."""
    from silica.agent.providers import get_reranker

    rr = get_reranker(config)
    if rr is None:
        return
    # Probe at PRODUCTION size, not "ping"/"pong". A served reranker rejects
    # per-request work that exceeds its physical batch (llama.cpp: 500 over
    # -ub, default 512 tokens) while still answering a tiny probe, so a
    # toy pair certifies a server that will abstain on every real call. Silica
    # sends _WINDOW_CHARS query + _WINDOW_CHARS document; probe exactly that.
    # Distinct pseudo-words, never a repeated phrase: BPE compresses repetition
    # so hard that "lorem ipsum" x N fits a batch that real prose blows past
    # (measured: 800+800 chars of vault text = 540-651 tokens, ~2.6 chars/token).
    # The probe has to be as token-dense as the content, or it certifies nothing.
    import itertools
    import string

    from silica.kernel.recall.rerank import _WINDOW_CHARS

    words = ("".join(t) for t in itertools.product(string.ascii_lowercase, repeat=3))
    probe_text = " ".join(itertools.islice(words, _WINDOW_CHARS))[:_WINDOW_CHARS]
    if rr.scores(probe_text, [probe_text]) is None:
        raise SystemExit(
            "reranker configured but not answering a production-sized pair "
            f"({_WINDOW_CHARS}-char query x {_WINDOW_CHARS}-char document): it "
            "would silently abstain and fake rerank == embed-only. If the server "
            "is up, its batch is too small (llama.cpp: -b/-ub >= -c). Otherwise "
            "start the reranker or pass --no-rerank.")


def warn_unpinned_provider(model: str, provider_pin: str | None) -> None:
    """Unpinned openrouter routing is nondeterministic even at temperature=0
    (proven: a byte-identical prompt flipped verdicts). Warn, do not fail —
    local backends legitimately have no provider concept.

    Falsy, not `is None`: CONFIG.openrouter_provider defaults to "", so an
    `is None` test made this guard dead for every caller that passed the config
    field straight through (probe_explain_rubric) — no pin, no warning, a
    nondeterministic run reported as an A/B.
    """
    if not provider_pin and str(model).startswith("openrouter/"):
        print("WARNING: openrouter model with no provider pin — unpinned routing "
              "is nondeterministic even at temperature=0. Set OPENROUTER_PROVIDER "
              "for a comparable A/B.", file=sys.stderr)


if __name__ == "__main__":
    # smallest check that the provenance block is well-formed and the
    # dataset hash actually reflects file content.
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "data.json"
        f.write_text(json.dumps({"x": 1}), encoding="utf-8")
        block = provenance(f, rid="RID")
        assert block["run_id"] == "RID"
        assert block["dataset"]["path"] == str(f)
        assert block["dataset"]["sha256"] == _sha256_file(f)
        f.write_text(json.dumps({"x": 2}), encoding="utf-8")
        assert provenance(f)["dataset"]["sha256"] != block["dataset"]["sha256"]
        assert provenance(Path(d) / "missing.json")["dataset"]["sha256"] is None
    print("ok")
