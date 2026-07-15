# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Cross-encoder rerank pass over a fused candidate pool.

The relatedness facade fuses embeddings + co-occurrence by RANK (RRF); neither leg
ever reads the query and a candidate *together*. A cross-encoder does exactly that,
scoring query x document jointly — the strongest precision lever after first-stage
recall. This module applies that pass to an already-retrieved pool: it reorders,
never retrieves, and abstains (leaves the pool's order untouched) whenever the
reranker is absent or errors, mirroring a down leg in the facade.

The reranker CLIENT lives in agent/providers.py (Reranker/get_reranker); this module
holds only the note-aware reorder, so the client stays a plain HTTP provider.
"""
import re
import statistics
from typing import Any, Callable

_WINDOW_CHARS = 800  # cross-encoder document budget (chars): excerpt window + gate unit
# ponytail: provisional pending phase-0 calibration (retrieval-gates spec 2026-07-14);
# separation is expected order-of-magnitude (wiki paragraphs vs chat sessions), so the
# exact factor is uncritical until the probe data freezes it.
_RERANK_WINDOW_FACTOR = 3
# Calibration hook: harnesses set it to capture {"median_len", "window", "fired"}
# per query; production leaves it None.
RERANK_GATE_PROBE: Callable[[dict], None] | None = None


def best_windows(text: str, query: str, width: int, n: int = 1) -> list[str]:
    """Up to `n` non-overlapping `width`-char slices of `text` densest in query
    terms, in document order (multi-window spec 2026-07-15).

    A cross-encoder sees ~512 tokens (~2k chars); on a long note the naive
    head slice `text[:width]` can miss the passage the query is actually about
    entirely, so the reranker scores irrelevant opening text and demotes a true
    match (measured: on LongMemEval's multi-turn chat sessions the head slice
    evicts gold sessions whose relevant turn sits past char 800). Anchoring
    windows on query-term density fixes that with no extra model call; on
    9-21k-char chat bodies a single window still cuts gold spans (gic 0.533 on
    the raw arm), so perception can ask for several.

    Greedy top-N with masking: hits per position never change (masking removes
    candidate positions, not text), so one density scan feeds every pick. The
    first window is always taken even at zero hits (n=1 stays bit-identical to
    the historical single-window behavior); each later window needs hits > 0 —
    never pad with irrelevant text, returning fewer than n windows is normal.
    Document order preserves chat chronology for temporal questions.
    """
    if len(text) <= n * width:
        return [text]
    terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 3}
    if not terms:
        return [text[:width]]
    low = text.lower()
    step = max(1, width // 4)
    candidates = [(pos, sum(low.count(t, pos, pos + width) for t in terms))
                  for pos in range(0, max(1, len(text) - width) + step, step)]
    chosen: list[int] = []
    while candidates and len(chosen) < n:
        pos, hits = max(candidates, key=lambda c: c[1])  # earliest max, as before
        if chosen and hits == 0:
            break
        chosen.append(pos)
        candidates = [c for c in candidates
                      if c[0] + width <= pos or c[0] >= pos + width]
    return [text[p:p + width] for p in sorted(chosen)]


def best_window(text: str, query: str, width: int) -> str:
    """The single `width`-char slice of `text` densest in query terms
    (see `best_windows`; this is its n=1 case, bit-identical)."""
    return best_windows(text, query, width, 1)[0]


_best_window = best_window  # transitional alias; drop once callers migrate


def _read_body(path: str, *, origin: str = "vault") -> tuple[str, str]:
    """(note name, full body text) for one note; ('', '') when unreadable —
    a length of 0 fails open toward reranking, and '' scores as irrelevant.
    origin='memory' (ADR-0019) resolves the path in the personal-memory vault,
    which the active-vault driver cannot open — so rerank never buries the lane."""
    if origin == "memory":
        from silica.kernel.memory_lane import memory_vault

        mv = memory_vault()
        if mv is None:
            return "", ""
        p = mv / (path if path.endswith(".md") else path + ".md")
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", ""
    else:
        try:
            from silica.driver import DRIVER

            content = DRIVER.read_note(path).content or ""
        except Exception:
            return "", ""
    from silica.kernel import frontmatter

    _data, _raw, body = frontmatter.split(content)
    name = path.rsplit("/", 1)[-1].removesuffix(".md")
    return name, (body or content)


def note_document(path: str, *, query: str = "", max_chars: int = _WINDOW_CHARS) -> str:
    """Title + body excerpt for one note, as the reranker's document text.

    With `query`, the excerpt is the query-densest ``max_chars`` window of the
    body (see `_best_window`) rather than the head slice, so a long note's
    relevant passage reaches the cross-encoder. Returns '' when the note is
    unreadable (the reranker scores '' as irrelevant, the right default for a
    candidate we cannot open).
    """
    name, text = _read_body(path)
    if not text:
        return ""
    excerpt = best_window(text, query, max_chars) if query else text[:max_chars]
    return f"{name}\n{excerpt}".strip()


def _path_of(item: Any) -> str:
    if isinstance(item, dict):
        return item.get("path", "")
    return getattr(item, "path", "")


def _origin_of(item: Any) -> str:
    if isinstance(item, dict):
        return item.get("origin", "vault")
    return getattr(item, "origin", "vault")


def rerank_related(
    reranker: Any,
    query_text: str,
    results: list,
    *,
    k: int,
    document_of: Callable[[Any], str] | None = None,
) -> list:
    """Reorder the first-stage top-k of `results` by cross-encoder relevance.

    Reorder-only (retrieval-gates spec, 2a): the pool is truncated to k BEFORE
    scoring, so membership belongs to the first stage and recall@k is
    rerank-invariant by construction — every measured rerank damage was
    selection damage, the only measured gain is ordering.

    Granularity abstain (2b): when the median candidate body dwarfs the
    cross-encoder window, the model cannot read the evidence and its ordering
    is noise — skip the call, keep first-stage order.

    Each result is any object/dict exposing a note path (`.path` or `["path"]`).
    Abstention — no reranker, empty query, gate fired, or the reranker erroring
    — falls back to the pool's existing order, so a disabled or down reranker
    is a pure no-op. `document_of(item) -> str` supplies each candidate's text
    (its lengths then feed gate 2b as-is); it defaults to reading the note.
    """
    pool = results[:k]
    if reranker is None or not pool or not query_text:
        return pool
    if document_of is not None:
        docs = [document_of(it) for it in pool]
        lengths = [len(d) for d in docs]
    else:
        bodies = [_read_body(_path_of(it), origin=_origin_of(it)) for it in pool]
        lengths = [len(text) for _name, text in bodies]
        docs = None
    median_len = statistics.median(lengths)
    fired = median_len > _RERANK_WINDOW_FACTOR * _WINDOW_CHARS
    if RERANK_GATE_PROBE:
        RERANK_GATE_PROBE({"median_len": median_len, "window": _WINDOW_CHARS,
                           "fired": fired})
    if fired:
        return pool
    if docs is None:
        docs = [f"{name}\n{best_window(text, query_text, _WINDOW_CHARS)}".strip()
                for name, text in bodies]
    scores = reranker.scores(query_text, docs)
    if scores is None or len(scores) != len(pool):
        return pool
    order = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)
    return [pool[i] for i in order]


def demo() -> None:
    """Self-check: reorder-only within top-k, granularity gate, abstain keeps order."""

    class _Fake:
        def __init__(self, s):
            self._s = s
            self.called = False

        def scores(self, query, documents):
            self.called = True
            return self._s

    items = [{"path": "a"}, {"path": "b"}, {"path": "c"}]
    doc = lambda it: it["path"]

    # 2a reorder-only: k=2 truncates the pool FIRST; b outscores a -> [b, a],
    # c can never be pulled in.
    out = rerank_related(_Fake([0.1, 0.9]), "q", items, k=2, document_of=doc)
    assert [i["path"] for i in out] == ["b", "a"], out

    # 2b granularity abstain: median doc >> window -> reranker never called.
    gate = _Fake([0.9, 0.1])
    long_doc = "x" * (_RERANK_WINDOW_FACTOR * _WINDOW_CHARS + 1)
    out = rerank_related(gate, "q", items, k=2, document_of=lambda it: long_doc)
    assert [i["path"] for i in out] == ["a", "b"] and not gate.called, out

    # reranker abstains (None) -> pool order, truncated
    out = rerank_related(_Fake(None), "q", items, k=2, document_of=doc)
    assert [i["path"] for i in out] == ["a", "b"], out

    # no reranker -> no-op passthrough, truncated
    out = rerank_related(None, "q", items, k=1, document_of=doc)
    assert [i["path"] for i in out] == ["a"], out

    # empty query -> no-op
    out = rerank_related(_Fake([0.9, 0.0, 0.0]), "", items, k=3, document_of=doc)
    assert [i["path"] for i in out] == ["a", "b", "c"], out

    # query-aware window: the relevant passage sits past a naive head slice, and
    # the window must surface it so the cross-encoder scores the right text.
    body = ("intro chatter " * 60) + "the user practices yoga for anxiety " + ("filler " * 60)
    assert len(body) > 800
    win = _best_window(body, "how often yoga for anxiety?", 200)
    assert "yoga for anxiety" in win, win
    assert _best_window("short body", "anything", 800) == "short body"  # no-op under width

    print("rerank demo ok")


if __name__ == "__main__":
    demo()
