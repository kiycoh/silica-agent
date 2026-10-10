# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Citations between paper leaves (ADR-0055 decision 6).

A derived edge, computed at graph build and never written into a note: the
leaf is the verbatim paper, and a derived edge refreshes itself the moment a
cited paper is imported. Ids only (DOI and arXiv), because they measured 96%
precision on the founder's library on 2026-10-05 while title matching is
unmeasured.

Kept out of `build_graph_data` on purpose: the report, the mindmap and the
learner read that payload too, and a missing paper is not a note to count. The
web graph adds this layer on top (`graph_view`).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from silica.sources.convert import _REFERENCES_HEADING_RE

# Cited by this many library papers and absent from the library ⇒ a missing
# paper node. Reopen at 100 such nodes (73 on 2026-10-05, ADR-0055 Reopen).
MISSING_MIN = 5

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>\]\[]+")
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)(\d{4}\.\d{4,5})(?:v\d+)?",
                       re.IGNORECASE)
_BARE_ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")
_TRAILING = ".,;:)"


def norm_id(raw: str) -> str:
    """`doi:<lowercased doi>` or `arxiv:<id without version>`, else "".

    An arXiv DOI (10.48550/arXiv.X) is the arXiv id X, so a paper the library
    holds by DOI and a reference that names its arXiv id meet.
    """
    s = (raw or "").strip()
    m = _ARXIV_RE.search(s) or _BARE_ARXIV_RE.match(s)
    if m:
        return f"arxiv:{m.group(1)}"
    m = _DOI_RE.search(s)
    if not m:
        return ""
    doi = m.group(0).rstrip(_TRAILING).lower()
    a = _ARXIV_DOI_RE.match(doi)
    return f"arxiv:{a.group(1)}" if a else f"doi:{doi}"


def ids_in(text: str) -> set[str]:
    out = {f"arxiv:{m.group(1)}" for m in _ARXIV_RE.finditer(text or "")}
    out |= {norm_id(m.group(0)) for m in _DOI_RE.finditer(text or "")}
    out.discard("")
    return out


def reference_text(body: str) -> str:
    """The text after the last References heading, or the whole body when the
    paper has none: a converter that lost the heading must not lose the ids."""
    lines = (body or "").splitlines()
    last = max((i for i, line in enumerate(lines) if _REFERENCES_HEADING_RE.match(line.strip())),
               default=None)
    return body if last is None else "\n".join(lines[last + 1:])


_TEXT_CACHE: dict[str, tuple[float, str]] = {}


def _read(path: Path) -> str:
    """Leaf text, cached by mtime: a graph build reads every paper leaf, and a
    paper is ~100 KB that changes only on re-import."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    hit = _TEXT_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8", errors="replace")
    _TEXT_CACHE[str(path)] = (mtime, text)
    return text


def _label_base(data: dict) -> str:
    authors = data.get("authors") or []
    first = str(authors[0]) if isinstance(authors, list) and authors else str(authors or "")
    surname = first.split(",")[0].strip() or str(data.get("title") or "?").split()[0]
    return f"{surname} {data.get('year') or 'n.d.'}"


def _cites_edge(n: int, src: str, dst: str) -> dict:
    return {"id": f"cites{n}", "from": src, "to": dst, "type": "CITES",
            "arrows": {"to": {"enabled": True, "scaleFactor": 0.5}}, "width": 1.0}


def paper_layer(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Mark paper leaves and derive their citations.

    Every leaf with a `zotero_key` becomes `type: "paper"`, labelled "Surname
    Year" (a/b by title on collision, as in a reference list) with its identity
    under `paper`. Returns the `CITES` edges and the missing-paper nodes; the
    caller appends both. Nothing else in `nodes` is touched.
    """
    from silica.config import CONFIG
    from silica.kernel.recall.paths import is_source_leaf
    from silica.kernel.write import frontmatter

    root = Path(CONFIG.vault_path or ".")
    papers: dict[str, tuple[set[str], set[str], str]] = {}
    by_label: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for n in nodes:
        if n.get("type") == "ghost" or not is_source_leaf(n.get("id", "")):
            continue
        data, _, body = frontmatter.split(_read(root / n["id"]))
        if not isinstance(data, dict) or not data.get("zotero_key"):
            continue
        refs = reference_text(body)
        ident = {norm_id(str(data.get(k) or "")) for k in ("doi", "arxiv")} - {""}
        papers[n["id"]] = (ident, ids_in(refs), refs)
        n["type"] = "paper"
        n["paper"] = {k: data[k] for k in ("zotero_key", "citekey", "title", "authors", "year",
                                           "venue", "doi", "arxiv") if data.get(k)}
        by_label[_label_base(data)].append((str(data.get("title") or ""), n))
    for base, group in by_label.items():
        group.sort(key=lambda t: t[0].casefold())
        for k, (_, n) in enumerate(group):
            n["label"] = base if len(group) == 1 else f"{base}{chr(ord('a') + k)}"

    owner = {i: pid for pid, (ident, _, _) in papers.items() for i in ident}
    edges: list[dict] = []
    cited_by: dict[str, list[str]] = defaultdict(list)
    for pid in sorted(papers):
        ids, cited, _ = papers[pid]
        for i in sorted(cited - ids):
            dst = owner.get(i)
            if dst is None:
                cited_by[i].append(pid)
            elif dst != pid:
                edges.append(_cites_edge(len(edges), pid, dst))

    missing: list[dict] = []
    for i, citing in sorted(cited_by.items()):
        if len(citing) < MISSING_MIN:
            continue
        node_id = f"__missing__{i}"
        kind, value = i.split(":", 1)
        quote = next((line.strip() for line in papers[citing[0]][2].splitlines()
                      if i in ids_in(line)), "")
        missing.append({
            "id": node_id, "type": "missing", "group": -1, "path": "",
            "label": f"arXiv {value}" if kind == "arxiv" else f"doi {value}",
            "title": quote[:300] or i,
            "quote": quote[:300],
            "href": (f"https://arxiv.org/abs/{value}" if kind == "arxiv"
                     else f"https://doi.org/{value}"),
            "cited_by": len(citing),
        })
        edges += [_cites_edge(len(edges) + k, src, node_id) for k, src in enumerate(citing)]
    return edges, missing


def _paper_leaves(vault: str | Path | None = None) -> dict[str, tuple[dict, set[str], set[str]]]:
    """{vault-relative leaf path: (frontmatter, own ids, cited ids)} for every
    paper leaf, read through the mtime cache."""
    from silica.config import CONFIG
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import load_manifest
    from silica.kernel.write import frontmatter

    vault = Path(vault or CONFIG.vault_path or ".")
    rel_root = Path(load_manifest(vault).write_dir or "") / SOURCES_DIR
    out: dict[str, tuple[dict, set[str], set[str]]] = {}
    for p in sorted((vault / rel_root).glob("@*.md")):
        data, _, body = frontmatter.split(_read(p))
        if not isinstance(data, dict) or not data.get("zotero_key"):
            continue
        ident = {norm_id(str(data.get(k) or "")) for k in ("doi", "arxiv")} - {""}
        out[(rel_root / p.name).as_posix()] = (data, ident, ids_in(reference_text(body)) - ident)
    return out


def cited_counts(vault: str | Path | None = None) -> Counter:
    """How many paper leaves of the vault cite each id: the Library's "cited
    by" column, for papers in the vault and outside it alike."""
    counts: Counter = Counter()
    for _, _, refs in _paper_leaves(vault).values():
        counts.update(refs)
    return counts


def paper_neighbours(leaf_rel: str) -> dict:
    """One paper's citations within the vault: the leaves it cites, the leaves
    citing it, and the ids it cites that no leaf holds, most cited first."""
    papers = _paper_leaves()
    if leaf_rel not in papers:
        return {}
    data, ident, refs = papers[leaf_rel]
    owner = {i: rel for rel, (_, own, _) in papers.items() for i in own}
    counts = Counter(i for _, _, r in papers.values() for i in r)
    outside = sorted((i for i in refs if i not in owner), key=lambda i: (-counts[i], i))
    return {
        "data": data,
        "cites": sorted({owner[i] for i in refs if i in owner and owner[i] != leaf_rel}),
        "cited_by": sorted(rel for rel, (_, _, r) in papers.items() if rel != leaf_rel and r & ident),
        "missing": [(i, counts[i]) for i in outside],
        "labels": {rel: _label_base(d) for rel, (d, _, _) in papers.items()},
    }

