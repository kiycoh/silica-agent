# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Image readings (ADR-0077 point 2): what the chat model saw in an image, once.

A reading is keyed by the SHA-256 of the image bytes and kept in the vault's
index namespace, never in a note: the vault is the person's, and a reading
written into it would pass for their words. A second map, basename -> sha, is
what lets `_note_text` and the lexical store swap an `![[name.png]]` embed for
its reading without being handed the note's path, so every embedding path (bulk
/embed, the query-time sweep, the write-time refresh, the dedup query text)
encodes the same text and none re-embeds what another just did.

Only `read_missing` calls the model, and only bulk /embed calls it: the sweep
runs on the query path, where 2 s an image is not an option.
The model call is handed in (`ask`), never imported: `_note_text` reaches this
module from the graph structure modules, which stay free of the agent (P2/DKB).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Raster formats a vision projector takes. SVG is a document and remote URLs
# are not the vault's, so both stay unread.
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

_EMBED_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]|!\[[^\]]*\]\(([^)\s]+)[^)]*\)")

# English on purpose: the prompt is the model's, and the reading's language is
# named in it. "Never 'This image'": 50 of 50 free-form 9B captions opened by
# naming the figure (2026-10-07), which is noise for BM25.
_PROMPT = (
    "Read this image for a study notebook. Reply with JSON only: "
    '{{"kind": "...", "description": "...", "text": "..."}}. '
    "kind: one or two words (diagram, chart, table, formula, screenshot, slide, photo, handwriting). "
    "description: what it shows, in one to three sentences in {language}; start with the content "
    "itself, never with 'This image' or 'The figure'. "
    "text: the words, labels and numbers written in it, verbatim, or an empty string."
)

# A whole page whose text layer is missing (ADR-0087 amendment), where
# _PROMPT is a figure's: on two paper pages rendered as pictures it gave back
# the figure or the table alone, 30 of 533 words and 20 of 504, and dropped the
# prose; this one returned 654 of a page's 673 words (Qwen3.5-9B, 2026-10-13).
PAGE_PROMPT = (
    "This is one page of a document whose text layer is missing. Reply with JSON only: "
    '{{"kind": "...", "description": "...", "text": "..."}}. '
    "text: every word printed on the page, verbatim and in reading order, headings, paragraphs, "
    "lists, table cells, captions and formulas included; keep paragraph breaks as blank lines; "
    "do not summarise or skip anything. "
    "kind: one or two words (slide, lecture notes, article page, table, handwriting). "
    "description: one sentence in {language} on what the page is about; start with the content "
    "itself, never with 'This page' or 'The image'."
)

# A page whose text layer lost its formulas (ADR-0087 amendment of 2026-10-14).
# Markdown, not PAGE_PROMPT's JSON: in JSON a lone \Sigma beside escaped ones
# failed the parse and lost 3 of 93 slide pages, and an unescaped \frac or
# \beta would parse as a form feed or a backspace. The dollar signs are asked
# for because 18 of the 93 came back as bare LaTeX, which Obsidian shows raw.
# Not formatted: it names no language, and "in its own language" keeps a
# slide Italian (scaledown/summarize answered 65 of the 93 in English).
FORMULA_PAGE_PROMPT = (
    "This is one page of a document, a slide or a page of notes, whose text layer lost its "
    "formulas. Transcribe it: every word printed on the page, verbatim, in reading order and in "
    "its own language, headings, lists, table cells and captions included; every formula in "
    "LaTeX, inline as $...$ and on a line of its own as $$...$$, never without the dollar signs. "
    "Keep paragraph breaks as blank lines. Do not summarise, translate, explain or skip "
    "anything. Reply with the transcription alone: no preamble, no code fence."
)

_cache: dict[str, object] = {"key": None, "store": None}


def _path() -> Path:
    from silica.kernel.recall import paths
    return paths.index_file("image_readings")


def _load() -> dict:
    p = _path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return {"readings": {}, "names": {}}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache["key"] != key:
        import orjson
        _cache["store"], _cache["key"] = orjson.loads(p.read_bytes()), key
    return _cache["store"]  # type: ignore[return-value]


def _save(store: dict) -> None:
    import orjson
    from silica.kernel.recall.paths import atomic_write_bytes
    atomic_write_bytes(_path(), orjson.dumps(store))


def _text(reading: dict) -> str:
    kind = (reading.get("kind") or "").strip()
    words = " ".join(s for s in ((reading.get("description") or "").strip(),
                                 (reading.get("text") or "").strip()) if s)
    return f"{kind}: {words}" if kind else words


def by_name() -> dict[str, dict]:
    """basename -> reading, for every image read in this vault."""
    store = _load()
    return {name: store["readings"][sha] for name, sha in store["names"].items()
            if sha and sha in store["readings"]}


def texts_by_name() -> dict[str, str]:
    """basename -> the reading as one line of words, for the index text.

    Cached with the store: `_note_text` asks once per note, 730 times a bulk
    pass on the test vault."""
    store = _load()
    if _cache.get("texts_of") is not store:
        _cache["texts"] = {name: _text(r) for name, r in by_name().items()}
        _cache["texts_of"] = store
    return _cache["texts"]  # type: ignore[return-value]


def _embedded(body: str) -> list[str]:
    """Basenames of the raster images `body` embeds, in order, once each."""
    out: list[str] = []
    for m in _EMBED_RE.finditer(body):
        raw = (m.group(1) or m.group(2) or "").strip()
        if raw.startswith(("http://", "https://")):
            continue
        name = os.path.basename(raw)
        if Path(name).suffix.lower() in _MIME and name not in out:
            out.append(name)
    return out


def texts_in(body: str) -> list[str]:
    """The readings of the images `body` embeds, for the lexical store."""
    if "![" not in body:
        return []
    texts = texts_by_name()
    return [texts[n] for n in _embedded(body) if n in texts]


def annotate(body: str) -> str:
    """`body` with each read image's reading on the line under its embed, for
    the agent's eyes only (silica_read_note); the note on disk is untouched."""
    if "![" not in body:
        return body
    readings = by_name()
    if not readings:
        return body
    out: list[str] = []
    for line in body.split("\n"):
        out.append(line)
        for name in _embedded(line):
            r = readings.get(name)
            # A note made from an image already holds its reading as prose
            # (convert.py, an image with no OCR text): once is enough.
            if r and (r.get("description") or "") not in body:
                out.append(f"> [figure, read by {r.get('model') or 'a model'}] {_text(r)}")
    return "\n".join(out)


def _model_name() -> str:
    from silica.config import CONFIG
    return (getattr(CONFIG, "vision_model", "") or getattr(CONFIG, "model", "") or "").rsplit("/", 1)[-1]


def image_part(path: Path) -> dict:
    """`path` as an OpenAI `image_url` content part. A data URL, because neither
    a local llama-server nor a hosted provider can open a vault path."""
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{_MIME[path.suffix.lower()]};base64,{data}"}}

def read_image(path: Path, language: str, *, ask: Callable[[list[dict]], str],
               prompt: str = _PROMPT) -> dict | None:
    """One reading of the image at `path`, or None when the reply is not one.

    `ask` sends the messages to a vision model and returns its text
    (`ask_chat_model` in silica/agent/vision.py); it is passed in because the
    graph structure modules that reach this one never import the agent (P2/DKB).
    Raises whatever `ask` raises: the caller decides whether a refused call
    means "no vision here" (stop) or "this image" (skip). `prompt` is
    `PAGE_PROMPT` for a whole page, the figure prompt otherwise."""
    from silica.kernel.text.sanitize import parse_json
    if path.suffix.lower() not in _MIME:
        return None
    reply = ask([{"role": "user", "content": [
        image_part(path), {"type": "text", "text": prompt.format(language=language)},
    ]}])
    try:
        parsed, _ = parse_json(reply, strict=False)
    except Exception:
        # A reply that is not JSON is a reading we cannot store; the image
        # stays unread and the next /embed asks again.
        return None
    if not isinstance(parsed, dict) or not (parsed.get("description") or parsed.get("text")):
        return None
    return {"kind": str(parsed.get("kind") or "").strip(),
            "description": str(parsed.get("description") or "").strip(),
            "text": str(parsed.get("text") or "").strip(),
            "model": _model_name(), "read_at": datetime.now().isoformat(timespec="seconds"),
            "by": "reader"}


def remember(path: Path, reading: dict) -> None:
    """Store a reading made outside a pass (convert.py) so /embed does not pay
    for the same image again."""
    store = _load()
    store = {"readings": dict(store["readings"]), "names": dict(store["names"])}
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    store["readings"][sha] = reading
    store["names"][path.name] = sha
    _save(store)

def _vault_images() -> dict[str, Path | None]:
    """basename -> the one raster image of that name in the vault, None when two
    files share it.

    ponytail: one walk per /embed pass and ambiguous basenames left unread;
    Obsidian names pasted images by timestamp and the converter by content hash
    plus a per-source prefix, so a collision is two files someone named alike.
    Resolve per note through the driver's asset index if that stops holding.
    """
    from silica.config import CONFIG
    root = Path(CONFIG.vault_path)
    found: dict[str, Path | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if Path(f).suffix.lower() in _MIME:
                found[f] = None if f in found else Path(dirpath) / f
    return found


def _language(body: str) -> str:
    from silica.kernel.text import language as lang_mod
    from silica.kernel.vault_manifest import get_active_manifest
    return (get_active_manifest().conventions.language
            or lang_mod.display_name(lang_mod.detect(body)) or "the language of the note")


def read_missing(notes: Iterable[tuple[str, str]], *, ask: Callable[[list[dict]], str]) -> int:
    """Read every image `notes` embed that has no reading yet; returns how many.

    `notes` is (path, body). The first image's call failing ends the pass: on a
    text-only endpoint every later call would fail the same way, and a vault
    with no vision endpoint gets no readings and indexes as it did before.
    """
    pending = [(name, body) for _path_, body in notes for name in _embedded(body)]
    if not pending:
        return 0
    store = _load()
    store = {"readings": dict(store["readings"]), "names": dict(store["names"])}
    files = _vault_images()
    read = 0
    first = True
    done: set[str] = set()
    for name, body in pending:
        path = files.get(name)
        if path is None or name in done:
            continue
        done.add(name)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        store["names"][name] = sha
        if sha in store["readings"]:
            continue
        try:
            reading = read_image(path, _language(body), ask=ask)
        except Exception as exc:
            if first:
                logger.warning("image reading off for this pass: the chat model refused %s (%s)", name, exc)
                break
            # One image the model chokes on (a corrupt file, an odd format)
            # must not cost the rest of the pass.
            logger.warning("image reading skipped %s: %s", name, exc)
            continue
        finally:
            first = False
        if reading:
            store["readings"][sha] = reading
            read += 1
            if read % 10 == 0:
                _save(store)  # a killed pass keeps what it paid for
    if read or store["names"] != _load()["names"]:
        _save(store)
    return read
