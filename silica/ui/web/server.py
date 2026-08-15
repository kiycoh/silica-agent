# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FastAPI backend for the localhost GUI.

Single in-memory session (localhost, one user, no auth). The critical seam is
sync `run_agent` (blocking) -> async SSE: run it in a worker thread and bridge
its callback events onto the event loop with `call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from silica.agent.constraints import web_turn_constraints
from silica.agent.loop import _is_tool_failure, run_agent
from silica.agent.recall_watch import THIN_COVERAGE_HINT, RecallWatch
from silica.config import CONFIG
from silica.kernel.recall.mindmap import note_resolver
from silica.sources.web_research import WebTurn
from silica.ui.web.callback import event_to_json, tool_calls_to_json

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# --- module-level session state (spec: single session) -----------------------
messages: list[dict] = []
current_cancel: threading.Event | None = None  # cancel token of the in-flight turn
current_task: asyncio.Task | None = None  # in-flight worker; owns the busy-gate release
_collapsed: set[int] = set()  # message indices elided by compaction, across turns
_busy = False  # one turn at a time; a second /chat is refused with 409
current_session_id: str | None = None  # file backing the live conversation, if saved
SESSIONS_DIR = Path.home() / ".silica" / "web_sessions"  # persisted chat transcripts


# Fresh-session seed, precomputed so /reset ("new chat") is instant instead of
# rebuilding the vault map + token count on the click path (~seconds on a real
# vault). Built at startup, refreshed in the background after each turn (the
# turn may have written notes). (messages, their token count).
_seed: tuple[list[dict], int] | None = None


def _build_seed() -> None:
    """Compute the fresh-session seed. Never touches the live session state:
    uses the pure token counter so a background rebuild can't clobber the
    context meter of the conversation in progress."""
    global _seed
    from silica.cli import _count_context_tokens, _inject_vault_map
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.prompts import system_prompt

    conv = get_active_manifest().conventions
    reply = conv.reply_language or conv.language
    msgs: list[dict] = [{"role": "system", "content": system_prompt(reply, math=True)}]
    _inject_vault_map(msgs)
    _seed = (msgs, _count_context_tokens(msgs))


def _prewarm_seed() -> None:
    """Refresh the seed off the request path; failures only cost freshness."""

    def work():
        try:
            _build_seed()
        except Exception:
            logger.exception("seed prewarm failed")

    threading.Thread(target=work, daemon=True).start()


def _reset_session() -> None:
    global current_cancel, current_task, _busy, current_session_id
    if _seed is None:
        _build_seed()
    seed_msgs, seed_tokens = _seed
    messages[:] = [dict(m) for m in seed_msgs]  # per-message copy; contents are never mutated
    CONFIG.context_tokens = seed_tokens
    _collapsed.clear()
    current_cancel = None
    current_task = None
    _busy = False
    current_session_id = None  # next turn opens a new file


def _capture_own_session() -> None:
    """Flush the live conversation to the capture WAL, if capture is on.

    The server owns the session, so the two moments it can see a conversation
    end are a new chat and its own shutdown. A closed tab is neither; the next
    one of these catches its content (accepted ceiling, spec §10).
    `capture_session` is opt-in and fail-open in itself — a capture bug can
    never break the GUI from here.
    """
    from silica.capture import capture_session

    capture_session(messages, session_id=current_session_id or uuid.uuid4().hex[:12],
                    driver="gui")


def _session_title(msgs: list[dict]) -> str:
    for m in msgs:
        if m.get("role") == "user" and m.get("content"):
            line = str(m["content"]).strip().splitlines()[0]
            return line[:57] + "…" if len(line) > 58 else line
    return "untitled"


def _save_session() -> None:
    """Persist the live conversation to SESSIONS_DIR/<id>.json (per vault).

    No-op until there's a user turn to name it. Called after every turn so a
    refresh/close never loses history; overwrites the same file in place.
    """
    global current_session_id
    if not any(m.get("role") == "user" and m.get("content") for m in messages):
        return
    if current_session_id is None:
        current_session_id = uuid.uuid4().hex
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "id": current_session_id,
        "title": _session_title(messages),
        "vault": CONFIG.vault_path or "",
        "updated": time.time(),
        "messages": messages,
    }
    # default=str: any non-JSON tool payload degrades to text rather than crash.
    (SESSIONS_DIR / f"{current_session_id}.json").write_text(
        json.dumps(record, default=str), encoding="utf-8"
    )


def _list_sessions() -> list[dict]:
    """Saved conversations for the current vault, newest first."""
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # skip corrupt/half-written files
        if rec.get("vault", "") != (CONFIG.vault_path or ""):
            continue
        out.append(
            {"id": rec.get("id"), "title": rec.get("title", "untitled"),
             "updated": rec.get("updated", 0)}
        )
    out.sort(key=lambda r: r["updated"], reverse=True)
    return out


import html as _html
import re
from urllib.parse import quote as _quote

# A whitespace-delimited path-like token: contains "/" or ends in ".md".
_PATHLIKE = re.compile(r"[^\s\[\]]*(?:/[^\s\[\]]*|\.md)")
_WIKILINK = re.compile(r"(!?)\[\[([^\]\[]+)\]\]")  # optional ! marks an embed
_TRAIL = ".,;:!?)"  # sentence punctuation to peel off a bare path token

# Vault attachments the drawer may inline; served only through /asset, only as
# <img> (so an SVG's scripts never execute — img context runs no JS).
_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

# --- OFM (Obsidian-flavored markdown) sugar ----------------------------------
# ==highlight== | #tag (letter-first, so #123 and hex colors stay literal)
_MARK_OR_TAG = re.compile(r"==([^=\n]+)==|(?<![\w#])#([A-Za-z_][\w/-]*)")
_COMMENT = re.compile(r"%%.*?%%", re.S)
_BLOCK_ID = re.compile(r"[ \t]+\^[\w-]+[ \t]*$", re.M)
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_CALLOUT_HEAD = re.compile(r"\[!(\w+)\][+-]?[ \t]*(.*)")  # first line of a callout quote
_TASK_HEAD = re.compile(r"^\[([ xX])\][ \t]+")  # first inline text of a task list item
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)


def _clean_name(ref: str) -> str:
    """Display name: basename without folders or `.md` (`a/b.md` -> `b`)."""
    return ref.rsplit("/", 1)[-1].removesuffix(".md")


def _anchor(path: str, display: str) -> str:
    return (
        f'<a class="note-link" data-path="{_html.escape(path, quote=True)}">'
        f"{_html.escape(display)}</a>"
    )


def _embed_img(target: str, alias: str) -> str:
    """<img> for a `![[file.png]]` embed; a numeric alias is Obsidian's width.
    ponytail: target is taken vault-root-relative — no shortest-name resolution
    for attachments; index attachment names if that ever bites."""
    src = "/asset?path=" + _quote(target)
    width = f' width="{alias}"' if alias.isdigit() else ""
    stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    alt = stem if alias.isdigit() or not alias else alias
    return f'<img src="{_html.escape(src, quote=True)}" alt="{_html.escape(alt, quote=True)}"{width}>'


_RAW_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def _rewrite_raw_img_src(html: str) -> str:
    """Route a raw-HTML ``<img src="assets/x.png">`` through /asset.

    markdown-it's commonmark preset passes raw HTML through, and the image
    rewrite in _render only reaches markdown-native ``![alt](path)`` tokens. A
    note written for GitHub uses the HTML form instead, so its src arrived
    intact and the browser resolved it against the page origin: the drawer
    404'd on every such image and showed the alt text in a box. Absolute,
    external, anchor and data: URLs pass untouched, same rule as the token
    path."""
    def sub(m: "re.Match[str]") -> str:
        src = m.group(3)
        if not src or src.startswith(("http://", "https://", "data:", "/", "#")):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}/asset?path={_quote(src)}{m.group(2)}"

    return _RAW_IMG_SRC.sub(sub, html)


def _linkify_text(text: str, resolve) -> str:
    """Turn resolvable note refs in one plain-text run into `.note-link` anchors.

    Two layers: wikilinks first (explicit `[[...]]` delimiters), then bare
    path-like tokens in the surviving prose. Unresolved wikilinks render like
    resolved ones but tagged `.broken` (no data-path — the click is a no-op);
    unresolved bare paths stay verbatim. `resolve=None` means plain escape.
    Returns an HTML fragment (safe parts escaped).
    """
    if resolve is None:
        return _html.escape(text)

    def link_paths(prose: str) -> str:
        out, pos = [], 0
        for m in _PATHLIKE.finditer(prose):
            out.append(_html.escape(prose[pos:m.start()]))
            tok = m.group(0)
            core = tok.rstrip(_TRAIL)
            tail = tok[len(core):]
            hit = resolve(core)
            if hit:
                out.append(_anchor(hit, _clean_name(core)) + _html.escape(tail))
            else:
                out.append(_html.escape(tok))
            pos = m.end()
        out.append(_html.escape(prose[pos:]))
        return "".join(out)

    out, pos = [], 0
    for m in _WIKILINK.finditer(text):
        out.append(link_paths(text[pos:m.start()]))
        bang, inner = m.group(1), m.group(2)
        target, _, alias = inner.partition("|")
        target, alias = target.strip(), alias.strip()
        # Obsidian subpath (#center alignment hint, #heading anchor): irrelevant
        # to serving a raster attachment, and it would defeat the ext check.
        target = target.split("#", 1)[0].strip()
        if bang and "." + target.rsplit(".", 1)[-1].lower() in _ASSET_EXTS:
            out.append(_embed_img(target, alias))
        else:
            hit = resolve(target)
            display = alias or _clean_name(target)
            if hit:
                out.append(_anchor(hit, display))
            else:
                out.append(f'<a class="note-link broken">{_html.escape(display)}</a>')
        pos = m.end()
    out.append(link_paths(text[pos:]))
    return "".join(out)


def _inline_ofm(text: str, resolve) -> str:
    """OFM inline sugar over one plain-text run: ==highlight== -> <mark>,
    #tag -> chip. Prose between matches still goes through note-ref linking."""
    out, pos = [], 0
    for m in _MARK_OR_TAG.finditer(text):
        out.append(_linkify_text(text[pos:m.start()], resolve))
        if m.group(1) is not None:
            out.append(f"<mark>{_linkify_text(m.group(1), resolve)}</mark>")
        else:
            out.append(f'<span class="tag">#{_html.escape(m.group(2))}</span>')
        pos = m.end()
    out.append(_linkify_text(text[pos:], resolve))
    return "".join(out)


def _ofm_blocks(tokens) -> None:
    """OFM block sugar, rewriting the token stream in place: ```mermaid fences
    become client-rendered <pre class="mermaid">, `> [!kind] title` blockquotes
    become callouts, and `- [ ]` list items become checkbox tasks."""
    from markdown_it.token import Token

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "fence" and tok.info.strip() == "mermaid":
            raw = Token("html_block", "", 0)
            raw.content = f'<pre class="mermaid">{_html.escape(tok.content)}</pre>\n'
            tokens[i] = raw
        elif tok.type == "math_block":
            raw = Token("html_block", "", 0)
            raw.content = f'<div class="math">{_mathml(tok.content, display=True)}</div>\n'
            tokens[i] = raw
        elif tok.type == "blockquote_open":
            j = next((k for k in range(i + 1, len(tokens)) if tokens[k].type == "inline"), None)
            kids = tokens[j].children if j is not None else None
            first = kids[0] if kids else None
            m = _CALLOUT_HEAD.match(first.content) if first is not None and first.type == "text" else None
            if m:
                kind = m.group(1).lower()
                tok.attrJoin("class", f"callout callout-{kind}")
                rest = kids[1:]
                if rest and rest[0].type == "softbreak":
                    rest = rest[1:]
                tokens[j].children = rest
                head = Token("html_block", "", 0)
                title = m.group(2).strip() or kind
                head.content = f'<p class="callout-title">{_html.escape(title)}</p>\n'
                tokens.insert(i + 1, head)
                i += 1  # skip the injected title
        elif (
            tok.type == "list_item_open"
            and i + 2 < len(tokens)
            and tokens[i + 1].type == "paragraph_open"
            and tokens[i + 2].type == "inline"
            and tokens[i + 2].children
        ):
            first = tokens[i + 2].children[0]
            m = _TASK_HEAD.match(first.content) if first.type == "text" else None
            if m:
                tok.attrJoin("class", "task")
                first.content = first.content[m.end():]
                box = Token("html_inline", "", 0)
                checked = " checked" if m.group(1) in "xX" else ""
                box.content = f'<input type="checkbox" disabled{checked}> '
                tokens[i + 2].children.insert(0, box)
        i += 1


def _mathml(tex: str, display: bool) -> str:
    """LaTeX -> MathML, rendered natively by the browser (no client JS/fonts).
    A failed conversion degrades to the escaped source in a code span."""
    try:
        from latex2mathml.converter import convert

        return convert(tex, display="block" if display else "inline")
    except Exception:
        fence = "$$" if display else "$"
        return f'<code class="math-err">{_html.escape(fence + tex + fence)}</code>'


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Pygments fence highlighting; empty string falls back to a plain fence.
    Token colors live in app.css, mapped onto the site palette."""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(lang)
    except Exception:  # no/unknown language — markdown-it escapes it plain
        return ""
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


# ponytail: fence-aware pre-pass, not full token-stream — %% inside inline
# `code spans` still strips; move into the markdown-it stream if that bites.
def _strip_ofm_meta(text: str) -> str:
    """Strip %%comments%% and trailing ^block-ids, sparing fenced code where
    %% and ^ are code (a lone %% in a fence would otherwise pair with a prose
    %% and swallow everything between)."""
    pieces: list[str] = []
    run: list[str] = []
    fence: tuple[str, int] | None = None  # (marker char, marker length)

    def _flush() -> None:
        if run:
            pieces.append(_BLOCK_ID.sub("", _COMMENT.sub("", "\n".join(run))))
            run.clear()

    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                _flush()
                fence = (m.group(1)[0], len(m.group(1)))
                pieces.append(line)
            else:
                run.append(line)
        else:
            pieces.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
    _flush()
    return "\n".join(pieces)


def _linkify(text: str, resolve=None) -> str:
    """Render markdown (+ OFM sugar) to HTML, linkifying resolvable note refs
    when `resolve` is given. Works on the markdown-it token stream, so
    `code_inline`/`fence` are separate token types and code is never linkified
    or tag-ified by construction."""
    from markdown_it import MarkdownIt
    from markdown_it.token import Token
    from mdit_py_plugins.dollarmath import dollarmath_plugin

    text = _strip_ofm_meta(text or "")
    md = (
        MarkdownIt(options_update={"highlight": _highlight})
        .enable("table")
        .enable("strikethrough")
        .enable("linkify")
    )
    md.options["linkify"] = True
    # fuzzy_link off or `nota.md` in prose resolves to the Moldovan ccTLD and
    # renders as a link to http://nota.md; fuzzy_email off keeps the scope at
    # "a URL is clickable", not "prose opens a mail client".
    md.linkify.set({"fuzzy_link": False, "fuzzy_email": False})
    # allow_space=False keeps prose prices ("$5 and $10") out of math
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False)
    tokens = md.parse(text)
    _ofm_blocks(tokens)
    for tok in tokens:
        if tok.type == "html_block":
            tok.content = _rewrite_raw_img_src(tok.content)
            continue
        if tok.type != "inline" or not tok.children:
            continue
        new = []
        # Note refs are suppressed inside an anchor. `_PATHLIKE` matches any token
        # with a slash, so the display text of a link (an autolinked URL is its own
        # text) hit the basename fallback in _resolve_in and came back as a note:
        # `https://en.wikipedia.org/wiki/chemistry` rendered as a `.note-link` to
        # `chemistry.md`, nested inside the <a href> the browser then unnests.
        # Whoever writes `[...](url)` has already said where the link points.
        depth = 0
        for child in tok.children:
            if child.type == "link_open":
                depth += 1
            elif child.type == "link_close":
                depth = max(0, depth - 1)
            if child.type == "html_inline":
                child.content = _rewrite_raw_img_src(child.content)
                new.append(child)
                continue
            if child.type == "image":
                # vault-relative image: route through /asset (absolute/external
                # and data: URLs pass untouched)
                src = child.attrGet("src") or ""
                if src and not src.startswith(("http://", "https://", "data:", "/")):
                    child.attrSet("src", "/asset?path=" + _quote(src))
                new.append(child)
                continue
            if child.type == "math_inline":
                raw = Token("html_inline", "", 0)
                raw.content = _mathml(child.content, display=False)
                new.append(raw)
                continue
            if child.type != "text":
                new.append(child)
                continue
            frag = _inline_ofm(child.content, None if depth else resolve)
            raw = Token("html_inline", "", 0)
            raw.content = frag
            new.append(raw)
        tok.children = new
    return md.renderer.render(tokens, md.options, {})


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading YAML frontmatter block. Returns (props, body); props is
    None unless the block parses to a mapping."""
    import yaml

    m = _FRONTMATTER.match(text or "")
    if not m:
        return None, text
    try:
        props = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    if not isinstance(props, dict):
        return None, text
    return props, text[m.end():]


def _render_frontmatter(props: dict) -> str:
    """Properties box for the note drawer: native <details>, one row per key,
    list values as individual chips."""
    rows = []
    for key, val in props.items():
        vals = val if isinstance(val, (list, tuple)) else [val]
        chips = "".join(
            f'<span class="fm-val">{_html.escape("" if v is None else str(v))}</span>'
            for v in vals
        )
        rows.append(
            f'<div class="fm-row"><span class="fm-key">{_html.escape(str(key))}</span>{chips}</div>'
        )
    return '<details class="fm" open><summary>properties</summary>' + "".join(rows) + "</details>"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _begin_turn() -> bool:
    """Claim the single-turn slot. Sync with no `await` between the test and the
    set, so two racing POSTs can't both pass. Returns False if one's in flight."""
    global _busy
    if _busy:
        return False
    _busy = True
    return True


def _end_turn() -> None:
    """Release the turn slot. Idempotent (a completed turn and its worker's
    done-callback may both call it)."""
    global _busy, current_cancel, current_task
    _busy = False
    current_cancel = None
    current_task = None


def _sweep_if_orphaned() -> None:
    """Free a gate claimed for a turn whose `run_turn` never ran — the client
    dropped between POST and the SSE body's first `__anext__`, so nothing else
    releases it. Runs after the response closes; a no-op once a worker exists."""
    if _busy and (current_task is None or current_task.done()):
        _end_turn()


async def run_turn(text: str) -> AsyncIterator[dict]:
    """One agent turn as a stream of transport-neutral wire dicts.

    Yields `event_to_json(...)` dicts as the agent streams, then exactly one
    terminal dict: `{"type": "done", ...}` or `{"type": "error", ...}`. Owns the
    whole turn lifecycle (session append, sync→async queue bridge, cancel token,
    context compaction, save). Both `--gui` (SSE) and `connect` (WS) consume this
    — the framing is the transport's job, not this core's.

    Gate lifecycle: the slot is freed on normal end/error at once; on abandonment
    (the consumer stops iterating — a dropped SSE/WS client) the worker keeps
    running, so we signal cancel and defer the release to the worker's exit, so
    no second turn overlaps a zombie still mutating `messages`.
    """
    from silica.cli import _compact_context, _expand_web_turn, _update_context_tokens

    global _busy, current_cancel, current_task, _collapsed
    if not _busy:  # direct callers (tests, future WS) that didn't pre-claim
        _busy = True
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    current_cancel = threading.Event()  # module-level so /stop can see it
    task: asyncio.Task | None = None

    def cb(ev):  # runs in the agent/LLM worker thread
        data = event_to_json(ev)
        if data is not None:
            loop.call_soon_threadsafe(q.put_nowait, data)

    # InjectorFSM phase transitions arrive on BUS, not through the agent
    # callback: they are emitted from inside the tool, several layers below the
    # loop that owns `cb`. Subscribed per turn and dropped in the finally, so a
    # turn never receives another turn's phases and nothing accumulates across
    # turns. Publishing happens on the FSM's thread, hence the same
    # call_soon_threadsafe hop `cb` uses.
    def on_phase(ev):
        try:
            cb(ev)
        except RuntimeError:
            pass  # loop already closed: the turn is gone, the event is moot

    from silica.agent.bus import BUS
    BUS.subscribe("work/phase", on_phase)

    try:
        # A slash command follows the REPL's dispatch order (silica/cli.py): the
        # direct handler first — synchronous, no LLM round-trip — then the
        # workflow expansion. Asking the same handler the REPL asks is the point:
        # the hand-kept list of "web commands" that used to gate this drifted, and
        # /lexical /wiki /graph /map /find /vault fell through it into an error.
        agent_msg = text
        # /web comes first: it is neither direct nor a workflow expansion but an
        # agent turn with web-only tools. A usage error raises ValueError, which
        # the except below turns into the single error event.
        web = _expand_web_turn(text, messages) if text.startswith("/") else None
        if web is not None:
            agent_msg = web[1]
        elif text.startswith("/"):
            from silica.cli import _expand_workflow_shortcut, _handle_direct_shortcut
            from silica.ui.console import CONSOLE

            def _run_slash():
                # Both dispatchers print their result to CONSOLE and both can do
                # real work, so both run under the capture and off the loop
                # thread. The expansion is not a pure string builder: /fetch,
                # /web-search and /convert do the whole job inside it and return
                # "" to say the REPL has nothing left for the agent. Reading
                # that "" as "not available" was reporting failure for work that
                # had already written notes to disk, with the success line going
                # to the server's stdout where no browser user can see it.
                with CONSOLE.capture() as capture:
                    handled = _handle_direct_shortcut(text, messages)
                    expanded = None if handled else _expand_workflow_shortcut(text)
                return handled or expanded == "", expanded, capture.get()

            handled, expanded, captured_out = await asyncio.to_thread(_run_slash)

            if handled:
                # Appended only once the verdict is in: a False falls through to
                # the agent below, which appends the expanded turn itself.
                messages.append({"role": "user", "content": text, "origin": "cli"})
                out = captured_out.strip()
                answer = f"```text\n{out}\n```" if out else "```text\n(done)\n```"
                messages.append({"role": "assistant", "content": answer})

                # Yield a fake agent turn with the direct result
                yield {
                    "type": "done",
                    "answer": answer,
                    "html": _linkify(answer, note_resolver()),
                    "context_tokens": CONFIG.context_tokens,
                    "max_context_tokens": CONFIG.max_context_tokens,
                }
                return

            if expanded is None:
                yield {"type": "error", "error": f"'{text}' not available in this session"}
                return
            agent_msg = expanded

        msg = {"role": "user", "content": agent_msg}
        if text.startswith("/"):
            msg["origin"] = "cli"
        messages.append(msg)

        # Both wrappers forward every event to `cb` untouched: WebTurn records the
        # trace the citations are built from, RecallWatch counts recall misses for
        # the thin-coverage hint.
        watch = WebTurn(web[0], cb) if web else RecallWatch(cb)

        sentinel = object()
        task = asyncio.create_task(
            asyncio.to_thread(
                run_agent, messages, CONFIG.model, watch,
                cancel_token=current_cancel,
                constraints=web_turn_constraints() if web else None,
            )
        )
        current_task = task
        task.add_done_callback(lambda t: q.put_nowait(sentinel))

        while True:
            item = await q.get()
            if item is sentinel:
                break
            yield item

        answer = await task  # re-raises if run_agent failed
        if web:
            # Before _linkify and before the compaction sweep: the Sources block
            # belongs to what the user sees AND to what the history carries.
            answer = watch.attribute(answer, messages)
        elif watch.web_answer:
            from silica.sources.web_research import relay_sources

            answer = relay_sources(answer, messages)
        _update_context_tokens(messages)
        _collapsed = _compact_context(messages, _collapsed)
        # note_resolver reads the DRIVER graph — with the ws backend installed
        # (silica connect) a driver call on the loop thread deadlocks (`_rpc`
        # blocks the very loop that must send the frame), so render off-loop.
        html = await asyncio.to_thread(lambda: _linkify(answer, note_resolver()))
        done = {
            "type": "done",
            "answer": answer,
            "html": html,
            "context_tokens": CONFIG.context_tokens,
            "max_context_tokens": CONFIG.max_context_tokens,
        }
        if not web and watch.thin:
            done["hint"] = THIN_COVERAGE_HINT  # muted line under the answer
        yield done
    except Exception as exc:  # never leave the UI stuck on the spinner
        logger.exception("web turn failed")
        yield {"type": "error", "error": str(exc)}
    finally:
        BUS.unsubscribe("work/phase", on_phase)
        _save_session()  # persist even on error so the user's turn isn't lost
        _prewarm_seed()  # the turn may have written notes — refresh the new-chat seed
        if task is not None and not task.done():
            current_cancel.set()  # abandonment: stop the zombie...
            task.add_done_callback(lambda t: _end_turn())  # ...free the gate when it exits
        else:
            _end_turn()  # normal / error / early-return: free now


def _turn_response(text: str) -> StreamingResponse:
    """One agent turn as an SSE stream. Caller must claim the slot via
    `_begin_turn()` first; `_sweep_if_orphaned` frees it if the body never runs."""

    async def gen():
        async for item in run_turn(text):
            yield _sse(item)

    return StreamingResponse(
        gen(), media_type="text/event-stream", background=BackgroundTask(_sweep_if_orphaned)
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Host the Obsidian bridge for the GUI session — the plugin dials in and
    the driver hot-swaps to ws (falls back on drop). No-op without the
    [connect] extra or when the vault has no .obsidian/."""
    from silica.ui.connect import maybe_start_bridge

    bridge = None
    try:
        bridge = await maybe_start_bridge()
    except Exception:
        logger.exception("bridge auto-start failed")  # the GUI must not die for it
    yield
    if bridge is not None:
        await bridge.stop()


app = FastAPI(lifespan=_lifespan)


@app.post("/chat")
async def chat(payload: dict):
    if not _begin_turn():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    return _turn_response(payload.get("text", ""))


@app.get("/supported_types")
def supported_types():
    """Extensions the nucleate picker offers — drives the `+` button's `accept`."""
    from silica.sources.registry import supported_nucleate_extensions

    return {"extensions": supported_nucleate_extensions()}


@app.get("/commands")
def list_commands():
    """Commands the web GUI's fuzzy picker offers — everything the chat turn can
    actually dispatch. `repl_only` ones are terminal-session business and would
    only answer 'not available in this session' if offered here."""
    from silica.ui.commands import COMMANDS

    return [
        {"name": c.name, "summary": c.summary, "usage": c.usage}
        for c in COMMANDS
        if not c.repl_only
    ]


async def _stage_uploads(files: list[UploadFile]) -> tuple[list[str], list[str]]:
    """Write uploads to Inbox and mechanically stage them, mirroring the inline
    half of `/nucleate` (silica/cli.py): PDFs convert to markdown, code/notebooks
    become skeleton stubs, prose stays as-is. Returns (ready, stubs): markdown
    notes ready for the injector/reading, and code stub note paths already
    written to the vault. The semantic step (nucleate? summarize?) is the agent's,
    driven by the user's message — see `_compose_nucleate_turn`.

    convert() shells out to mineru (can be minutes on a book) and stage() reads
    whole files, so both run in a worker thread: on the loop thread they blocked
    /stop for the whole conversion, leaving a visible Stop button that could not
    be served.
    """
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.sources.convert import convert
    from silica.sources.registry import adapter_for, stage

    inbox = Path(CONFIG.vault_path or ".") / "Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    enabled = get_active_manifest().sources
    ready: list[str] = []
    stubs: list[str] = []
    for f in files:
        dest = inbox / Path(f.filename or "dropped").name
        dest.write_bytes(await f.read())
        rel = f"Inbox/{dest.name}"
        adapter = adapter_for(rel, enabled=enabled)
        if adapter is None:  # no source claims it → converter fallback (PDF today)
            try:
                ready.extend(await asyncio.to_thread(convert, rel))
            except ValueError as exc:
                logger.warning("nucleate: skipped %s: %s", dest.name, exc)
            continue
        result = await asyncio.to_thread(stage, adapter, rel)
        if result["status"] == "distill":       # prose → injector re-reads it
            ready.append(rel)
        elif result["status"] == "ok":            # code/notebook → stub written
            stubs.append(result["note_path"])
        else:
            logger.warning("nucleate: %s: %s", dest.name, result.get("message", ""))
    return ready, stubs


def _compose_nucleate_turn(text: str, ready: list[str], stubs: list[str]) -> str:
    """The agent turn for a batch of attached files: the user's instruction plus
    a factual manifest of what got staged. Empty instruction defaults to nucleate."""
    lines: list[str] = []
    if ready:
        lines.append("Markdown staged in Inbox, ready to nucleate or read:")
        lines += [f"- {p}" for p in ready]
    if stubs:
        lines.append("Code skeleton stubs already staged in the vault:")
        lines += [f"- {p}" for p in stubs]
    manifest = "\n".join(lines) if lines else "(no files could be staged)"
    base = text.strip() or (
        "Nucleate the attached file(s) into an appropriate folder; "
        "ask me if the target is unclear."
    )
    return f"{base}\n\n---\nAttached files:\n{manifest}"


@app.post("/nucleate")
async def nucleate(files: list[UploadFile] = File(...), text: str = Form("")):
    if not _begin_turn():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    try:
        ready, stubs = await _stage_uploads(files)
    except Exception:
        _end_turn()  # release the slot the staging never got to use
        raise
    return _turn_response(_compose_nucleate_turn(text, ready, stubs))


# How many rows of an uncapped list the metrics view receives. The report caps
# its own ranked lists at top_k; orphans and dangling are exhaustive, so they get
# cut here — and the true length always rides along in `totals`, so a cut list
# can never read as "this is all of them".
#
# 12, not 60: at 60 the orphans and dangling cards ran to 60 rows each and the
# dashboard became two long lists with charts above them (8.5k px on a 686-note
# vault). A card samples; GRAPH_REPORT.md is where the full list lives.
_METRICS_ROWS = 12

# Degree-distribution buckets. Doubling widths, not equal ones: a wikilink graph
# is heavy-tailed, so linear bins put ~everything in the first two and stretch a
# hundred empty bins under the hubs. The first three degrees stay their own bin
# because 0 (isolated), 1 (a leaf) and 2 mean different things about a note.
_DEGREE_BINS = ((0, 0), (1, 1), (2, 2), (3, 4), (5, 8), (9, 16), (17, 32), (33, 64), (65, None))


def _degree_histogram(degree_map: dict[str, int]) -> list[dict]:
    """Bucket every note's resolved-link degree. Trailing empty buckets are
    dropped so the axis ends where the vault does; interior empties stay, since
    a hole in the middle of the distribution is itself the reading."""
    out = []
    for lo, hi in _DEGREE_BINS:
        n = sum(1 for d in degree_map.values() if d >= lo and (hi is None or d <= hi))
        label = str(lo) if hi == lo else (f"{lo}+" if hi is None else f"{lo}-{hi}")
        out.append({"label": label, "count": n, "lo": lo})
    while len(out) > 1 and out[-1]["count"] == 0:
        out.pop()
    return out


def _shape_reading(adj: dict, deg: dict, areas: list[dict], label_of: dict, stops: int = 24) -> list[dict]:
    """A reading order over the vault, derived rather than authored.

    Areas biggest first, and inside one the hub then its best-connected
    neighbours, breadth-first. Not a ranking of what matters: it is the order
    that keeps each next note adjacent to something already read, which is the
    only property a reading path can actually promise from link structure.

    Capped, because a path with 795 stops is the file tree with extra words.
    """
    out: list[dict] = []
    seen: set[str] = set()
    # Every area's hub, reserved. Hubs link to each other, so without this the
    # second area's hub gets picked up as a neighbour of the first, and then its
    # own section is skipped as already-seen — silently dropping the area, and
    # the biggest ones first, since those are exactly the well-connected hubs.
    hubs = {a["path"] for a in areas if a["path"]}
    covered = 0
    for a in areas:
        hub = a["path"]
        if not hub:
            continue
        if len(out) + 1 > stops:
            break
        covered += 1
        seen.add(hub)
        out.append({"path": hub, "label": label_of.get(hub, hub), "area": a["label"],
                    "why": f"hub of {a['label']} — {a['size']} notes, the densest point of the area"})
        # Two neighbours per area: enough to show what the hub opens onto,
        # few enough that the biggest area cannot eat the whole path.
        near = sorted((n for n in adj.get(hub, ()) if n not in seen and n not in hubs),
                      key=lambda n: -deg.get(n, 0))[:2]
        for n in near:
            if len(out) >= stops:
                break
            seen.add(n)
            out.append({"path": n, "label": label_of.get(n, n), "area": a["label"],
                        "why": f"linked from the hub, {deg.get(n, 0)} links of its own"})
    return {"stops": out, "areas_covered": covered, "areas_total": len(areas)}


@app.get("/shape")
def shape():
    """Three read-only views over ONE graph build: containment, area coupling,
    and a derived reading order.

    They share an endpoint because they share the expensive part — build_graph_data
    plus community detection — and splitting them into three routes would pay it
    three times for surfaces a reader flips between.
    """
    from silica.kernel.recall.graph_export import build_graph_data, detect_communities

    try:
        nodes, edges = build_graph_data(folder="")
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        logger.warning("shape: graph build failed (%s)", exc)
        return {"error": str(exc)}

    real = [n for n in nodes if n.get("type") != "ghost"]
    group_of = {n["id"]: n.get("group", -1) for n in real}
    label_of = {n["id"]: n.get("label") or n["id"].rsplit("/", 1)[-1] for n in real}

    adj: dict[str, set[str]] = {}
    deg: dict[str, int] = {}
    intra: dict[int, int] = {}
    inter: dict[tuple[int, int], int] = {}
    # Unordered pairs, counted once. A wikilink is directed and a mutual pair is
    # two edges, so counting edges made cohesion exceed 1 on any small area whose
    # notes link both ways — a 2-note area with a mutual link scored 2.0 on a
    # ratio bounded in [0, 1]. `compute_report` walks `G_und.edges()` for exactly
    # this reason; deduping here is what puts the two on one currency. The
    # off-diagonal therefore counts LINKED PAIRS of notes, not wikilinks.
    seen_pairs: set[tuple[str, str]] = set()
    for e in edges:
        if e.get("type") != "EXTRACTED":
            continue
        a, b = e.get("from"), e.get("to")
        ga, gb = group_of.get(a), group_of.get(b)
        if ga is None or gb is None or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
        if ga == gb:
            if ga >= 0:
                intra[ga] = intra.get(ga, 0) + 1
        elif ga >= 0 and gb >= 0:
            inter[(min(ga, gb), max(ga, gb))] = inter.get((min(ga, gb), max(ga, gb)), 0) + 1

    sizes: dict[int, int] = {}
    for g in group_of.values():
        if g >= 0:
            sizes[g] = sizes.get(g, 0) + 1

    # Multi-note communities only, biggest first. A singleton is its own
    # community: on the matrix it is a row and column of zeroes with a perfect
    # diagonal, which is 65 rows of noise around the 26 that carry the vault.
    ids = sorted((g for g, s in sizes.items() if s > 1), key=lambda g: (-sizes[g], g))
    hub_of: dict[int, str] = {}
    for g in ids:
        members = [n for n in real if group_of.get(n["id"]) == g]
        best = max(members, key=lambda n: deg.get(n["id"], 0), default=None)
        if best:
            hub_of[g] = best["id"]

    def cohesion(g: int) -> float:
        s = sizes[g]
        possible = s * (s - 1) / 2
        return round(intra.get(g, 0) / possible, 4) if possible else 0.0

    areas = [
        {"id": g, "label": label_of.get(hub_of.get(g, ""), f"#{g}"), "path": hub_of.get(g, ""),
         "size": sizes[g], "cohesion": cohesion(g), "intra": intra.get(g, 0)}
        for g in ids
    ]
    matrix = [[(intra.get(a, 0) if a == b else inter.get((min(a, b), max(a, b)), 0))
               for b in ids] for a in ids]

    return {
        "areas": areas,
        "matrix": matrix,
        # Every real note, for the containment view. Three fields, not the whole
        # node: the treemap needs a path, an area and a weight, and shipping the
        # colour/font/title the canvas uses would triple the payload for nothing.
        "notes": [{"path": n["id"], "size": n.get("size") or 1, "area": group_of.get(n["id"], -1)}
                  for n in real],
        "reading": _shape_reading(adj, deg, areas, label_of),
        "totals": {"notes": len(real), "areas": len(ids),
                   "singletons": sum(1 for s in sizes.values() if s <= 1)},
    }


def _write_sessions(report) -> dict | None:
    """The days claims were written, crossed against the areas that received them.

    Not a chronology of the vault: the vault's clock is per-claim (`valid_from`
    stamps), and only a nucleated note carries one. So this measures what WROTE
    the vault, not when the vault's subjects happened.

    A session x area matrix, not a time axis. Measured on a real vault the dates
    collapse onto 9 days inside a 2-month window with one straggler two years
    back; on a linear date axis that straggler takes 90% of the width and the
    nine days that hold 99% of the work land in a smear. The matrix drops the
    duration and keeps what varies -- which areas recur across sessions.

    Areas are the multi-note communities only. A singleton is its own community,
    so counting them would make every session look perfectly focused by
    construction. Areas never written into are counted rather than listed: the
    reading here is coverage, and 19 empty columns bury the 7 carrying the work.

    A stem that resolves to more than one note (the vault has forked pairs
    sharing a subpath under `write_dir`) is attributed to no area and counted as
    ambiguous, because guessing one of the two would silently move a mark into
    the wrong column.
    """
    from silica.kernel.write.timeline import timeline

    vault = Path(CONFIG.vault_path or "").expanduser()
    if not vault.is_dir():
        return None
    rows = timeline(vault, limit=10**6)["rows"]
    if not rows:
        return None

    areas = [c for c in report.clusters if c.size > 1]
    # Stem -> the areas claiming it. A set, so a genuine fork shows up as >1
    # rather than as whichever member the iteration happened to reach last.
    by_stem: dict[str, set[int]] = {}
    for c in areas:
        for m in c.members:
            by_stem.setdefault(m.rsplit("/", 1)[-1].removesuffix(".md"), set()).add(c.cluster_id)
    hub_path = {c.cluster_id: (c.hub or "") for c in areas}

    days: dict[str, dict[str, int]] = {}
    touched: dict[int, int] = {}
    ambiguous = unplaced = 0
    for date, _label, stem in rows:
        hit = by_stem.get(stem)
        if not hit:
            unplaced += 1
            continue
        if len(hit) > 1:
            ambiguous += 1
            continue
        cid = next(iter(hit))
        cells = days.setdefault(date, {})
        cells[str(cid)] = cells.get(str(cid), 0) + 1
        touched[cid] = touched.get(cid, 0) + 1

    if not days:
        return None
    # Busiest area first: the columns are read left to right, and the areas the
    # writing actually lands in are the ones worth seeing without scrolling.
    ordered = sorted(touched.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "areas": [
            {"id": str(cid), "label": (hub_path.get(cid, "") or f"#{cid}").rsplit("/", 1)[-1]
             .removesuffix(".md"), "path": hub_path.get(cid, ""), "total": n}
            for cid, n in ordered
        ],
        "days": [
            {"date": d, "notes": sum(cells.values()), "cells": cells}
            for d, cells in sorted(days.items())
        ],
        "areas_total": len(areas),
        "untouched": len(areas) - len(touched),
        # The notes with no claim clock at all. Named, never dropped: on a vault
        # written mostly by hand this is the overwhelming majority, and a matrix
        # that omits it reads as "the whole vault, over 9 days".
        "undated": max((report.totals or {}).get("notes", 0) - len(rows), 0),
        "ambiguous": ambiguous,
        "unplaced": unplaced,
    }


@app.get("/metrics")
def metrics(proposals: bool = False):
    """Everything the L1 graph report measures, as JSON for the metrics tab.

    Two depths, because the co-occurrence leg costs an order of magnitude more
    than the rest and, unlike the rest, grows with the square of the vault
    (_compute_cooccur_delta ranks every note against every other):

      default          — analytics + embeddings (~2s on a 686-note vault).
                         `depth: "structural"`.
      ?proposals=1     — adds the co-occurrence delta (autolink candidates,
                         stale links, missing hubs, integration deficits).
                         ~7s on the same vault. `depth: "full"`.

    The depth rides in the payload because E(vault) is only comparable across
    reports built at the same depth (see vault_energy): its `deficits` term is
    zero without the co-occurrence leg, and on a real vault that term dominates.
    The client labels the number rather than letting two different E's look alike.
    """
    from silica.kernel.report.graph_report import compute_report
    from silica.kernel.report.vault_energy import vault_energy

    try:
        report = compute_report(
            analytics=True, with_embeddings=True, with_cooccurrence=proposals, top_k=20,
        )
    except Exception as exc:
        logger.warning("metrics: report failed (%s)", exc)
        return {"error": str(exc)}

    e = vault_energy(report)
    short = lambda nid: (nid or "").rsplit("/", 1)[-1]  # noqa: E731
    # An area is named by its hub note's *name*: the full store path is a folder
    # tree, and in a table cell it wraps to three lines and says nothing extra.
    label = {c.cluster_id: (short(c.hub) or f"#{c.cluster_id}") for c in report.clusters}
    size = {c.cluster_id: c.size for c in report.clusters}

    return {
        "path": CONFIG.vault_path or "",
        "generated_at": report.generated_at,
        "depth": "full" if proposals else "structural",
        "totals": report.totals,
        "discourse_state": report.discourse_state,
        "energy": {
            "total": round(e.total, 2),
            # Ordered as E is composed: the one negative (bond-forming) term
            # first, then the entropic costs. `deficits` is dropped rather than
            # printed as 0.00 when the leg that measures it never ran — a zero
            # would read as "measured, came out flat". It contributes 0.0 either
            # way, so the terms still sum to `total`.
            "terms": [
                {"name": "cohesion", "value": round(e.cohesion, 2)},
                {"name": "orphans", "value": round(e.orphans, 2)},
                {"name": "dangling", "value": round(e.dangling, 2)},
                {"name": "gaps", "value": round(e.gaps, 2)},
                *([{"name": "deficits", "value": round(e.deficits, 2)}] if proposals else []),
                {"name": "contested", "value": round(e.contested, 2)},
            ],
        },
        "degree_histogram": _degree_histogram(report.degree_map),
        "clusters": [
            {"id": c.cluster_id, "size": c.size, "hub": short(c.hub), "path": c.hub,
             "cohesion": c.cohesion}
            for c in sorted(report.clusters, key=lambda c: -c.size)
        ],
        "hubs": [
            {"label": n.label, "path": n.id, "area": label.get(n.cluster, f"#{n.cluster}"),
             "degree": n.degree, "in": n.in_degree, "out": n.out_degree,
             "betweenness": n.betweenness}
            for n in report.god_nodes
        ],
        "bridges": [
            {"source": short(b.source), "target": short(b.target),
             "source_path": b.source, "target_path": b.target, "weight": b.weight}
            for b in report.bridges
        ],
        # The two area sizes ride along because they *are* the ranking:
        # gap_score = size_a * size_b / (1 + inter_edges). gap_density is left
        # out — on a real vault it reads 99.7-100% on every row, and a column
        # that never varies cannot explain the order it is sitting in.
        "gaps": [
            {"a": short(g.hub_a), "b": short(g.hub_b), "a_path": g.hub_a, "b_path": g.hub_b,
             "inter_edges": g.inter_edges, "size_a": size.get(g.cluster_a, 0),
             "size_b": size.get(g.cluster_b, 0)}
            for g in report.structural_gaps
        ],
        "orphans": [{"label": short(p), "path": p} for p in report.orphans[:_METRICS_ROWS]],
        "dangling": report.dangling[:_METRICS_ROWS],
        "contested": [
            {"label": short(c.path), "path": c.path, "refs": c.refs} for c in report.contested
        ],
        "source_drift": [
            {"label": short(d.note), "path": d.note, "source": d.source}
            for d in report.source_drift[:_METRICS_ROWS]
        ],
        "attention": [
            {"label": short(a.path), "path": a.path, "days_idle": a.days_idle,
             "degree": a.degree, "misses": a.misses, "attempts": a.attempts,
             "score": round(a.score, 2)}
            for a in report.attention_candidates
        ],
        "deficits": [
            {"label": short(d.path), "path": d.path, "concepts": d.concepts,
             "degree": d.degree, "score": round(d.score, 2)}
            for d in report.integration_deficits
        ],
        # Confirmed first — those are the merge candidates; the borderline band
        # is only "link, don't merge". Neither list is capped by the report, and
        # on a real vault they run to the hundreds, so the slice happens here.
        "duplicates": ([
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": True}
            for d in report.confirmed_duplicate_pairs
        ] + [
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": False}
            for d in report.duplicate_pairs
        ])[:_METRICS_ROWS],
        # Sliced like every other uncapped list: the report caps the
        # co-occurrence leg at top_k, but the import-derived candidates
        # _compute_code_signals appends are exhaustive — 13k pairs on a
        # 400-note vault, which is a 4 MB payload and a card 390,000 px tall.
        # The true count rides in `totals`, so the cut list can't read as all.
        "autolinks": [
            {"a": short(a.source), "b": short(a.target), "a_path": a.source,
             "b_path": a.target, "weight": a.weight, "shared": a.shared[:4]}
            for a in report.autolink_candidates[:_METRICS_ROWS]
        ],
        "stale_links": [
            {"a": short(s.source), "b": short(s.target), "a_path": s.source, "b_path": s.target}
            for s in report.stale_links
        ],
        "missing_hubs": [
            {"concept": h.concept, "centrality": round(h.centrality, 3)}
            for h in report.missing_hubs
        ],
        "lean_notes": [{"label": short(p), "path": p} for p in report.lean_notes[:_METRICS_ROWS]],
        "temporal": (
            {
                "notes_scanned": report.temporal.notes_scanned,
                "by_tier": {str(k): v for k, v in report.temporal.by_tier.items()},
                "stamped": report.temporal.stamped,
                "superseded_notes": report.temporal.superseded_notes,
                "superseded_sections": report.temporal.superseded_sections,
                "oldest_valid_from": report.temporal.oldest_valid_from,
            }
            if report.temporal and report.temporal.notes_scanned
            else None
        ),
        "sessions": _write_sessions(report),
        "code_coverage": (
            {
                "documented": report.code_coverage.documented,
                "total": report.code_coverage.total,
                "undocumented": [
                    {"path": p, "fan_in": f}
                    for p, f in report.code_coverage.undocumented[:_METRICS_ROWS]
                ],
            }
            if report.code_coverage and report.code_coverage.total
            else None
        ),
    }


@app.get("/graph")
def graph():
    import tempfile

    from silica.tools import TOOLS

    out = Path(tempfile.gettempdir()) / "silica_web_graph.html"  # regenerated each request
    try:
        TOOLS["silica_graph_export"].run(output_path=str(out), folder="")
        return HTMLResponse(out.read_text(encoding="utf-8"))
    except Exception as exc:
        return HTMLResponse(f"<p style='font-family:monospace'>graph unavailable: {exc}</p>")


@app.get("/map")
def mindmap(note: str = ""):
    """Static-SVG radial map rooted on `note` — ephemeral, in-session (not written).

    Consumes the same precomputed positions as the .canvas serializer, so the two
    surfaces cannot diverge. Empty/unknown note degrades to a message, like /graph.
    """
    from silica.config import CONFIG
    from silica.kernel.recall.mindmap import (
        build_mapview,
        gather_materials,
        note_resolver,
        render_map_svg,
    )

    if not note.strip():
        return HTMLResponse("<p style='font-family:monospace;color:#8a93a3'>enter a note: /map?note=…</p>")
    try:
        # Accept a title or a path — the input field usually gives a title.
        root = note_resolver()(note)
        if root is None:
            return HTMLResponse(
                f"<p style='font-family:monospace;color:#8a93a3'>'{note}' not found in vault.</p>"
            )
        materials = gather_materials(root, latent_k=CONFIG.mindmap_latent_k)
        mv = build_mapview(
            root, materials, max_nodes=CONFIG.mindmap_max_nodes, hops=CONFIG.mindmap_hops
        )
        if len(mv.nodes) <= 1:
            return HTMLResponse(
                f"<p style='font-family:monospace;color:#8a93a3'>'{root}' has no neighbors to map "
                "(isolated in the graph).</p>"
            )
        return HTMLResponse(render_map_svg(mv, title=f"map · {root}"))
    except Exception as exc:
        return HTMLResponse(f"<p style='font-family:monospace'>map unavailable: {exc}</p>")


@app.get("/find")
def find(q: str = "", k: int = 5):
    """Direct semantic-search panel: calls the tool straight, same pattern as /graph and /map."""
    from silica.tools import TOOLS

    q = q.strip()
    if not q:
        return HTMLResponse("<p style='font-family:monospace;color:#8a93a3'>usage: /find &lt;query&gt; [--k=N]</p>")
    try:
        parsed = json.loads(TOOLS["silica_semantic_search"].run(query=q, k=k))
    except Exception as exc:
        return HTMLResponse(f"<p style='font-family:monospace'>find unavailable: {exc}</p>")
    if "error" in parsed:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>{_html.escape(parsed['error'])}</p>")
    results = parsed.get("results", [])
    if not results:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>no results for '{_html.escape(q)}'.</p>")
    rows = []
    for r in results:
        p = r.get("path") or r.get("name") or "?"
        rows.append(
            f'<div class="find-result">{_anchor(p, _clean_name(p))}'
            f'<span class="find-score">{r.get("score", 0.0):.3f}</span></div>'
        )
    return HTMLResponse("".join(rows))


@app.get("/note")
def note(path: str = ""):
    """Read-only rendered note for the drawer. Graceful on miss (never 500).

    Only keys present in the vault index resolve, so an out-of-vault `path`
    falls through to the graceful message — path traversal is closed for free.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        return {"title": path, "html": "<p>note not found in vault.</p>"}
    try:
        content = get_driver().read_note(NoteRef(name=_clean_name(canon), path=canon)).content
    except Exception:
        return {"title": _clean_name(canon), "html": "<p>note unreadable.</p>"}
    props, body = _split_frontmatter(content)
    html = _linkify(body, resolve)
    if props:
        html = _render_frontmatter(props) + html
    return {"title": _clean_name(canon), "html": html}


# --- what this session changed (GET /changes, /changes/diff) ------------------
# The ledger is the driver's (silica.kernel.write.session_changes): the note as it
# stood before silica first touched it. The *after* side is read off disk on every
# request, so the list is never a claim about the past — it is the difference
# between then and the file as it is right now, and an /undo empties a row by
# putting the bytes back rather than by anyone remembering to remove it.

_DIFF_CONTEXT = 3
# ponytail: a hard line cap, tail dropped with a count. Past a few hundred lines a
# diff stops being reviewable in a drawer and the note itself is one click away.
_MAX_DIFF_LINES = 800
# difflib opens every diff with a hunk header, but a gap marker only *means*
# something when lines were skipped above it — which is not the case when the
# first hunk starts at the top of the file (or at 0, for a create or a delete).
_HUNK_AT_TOP = re.compile(r"^@@ -[01](?:,\d+)? \+[01](?:,\d+)? @@")


def _read_note_text(rel: str) -> str | None:
    """The note's bytes as they are now, or None if it is no longer there."""
    try:
        return (Path(CONFIG.vault_path) / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def _tally(before: str, after: str) -> tuple[int, int]:
    """Lines added and removed — the same opcodes the unified diff walks."""
    import difflib

    added = removed = 0
    sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _kind(before: str | None, after: str | None, origin: str | None, changed: bool) -> str:
    if before is None:
        return "created"
    if after is None:
        return "deleted"
    return "moved" if origin and not changed else "modified"


def _change_rows() -> list[dict]:
    from silica.kernel.write import session_changes

    rows = []
    for path, base in session_changes.snapshot().items():
        after = _read_note_text(path)
        if base.before is None and after is None:
            continue  # created and then rolled back: nothing happened
        added, removed = _tally(base.before or "", after or "")
        if not (added or removed or base.origin):
            continue  # written with the same bytes it already had
        rows.append({
            "path": path,
            "name": _clean_name(path),
            "kind": _kind(base.before, after, base.origin, bool(added or removed)),
            "added": added,
            "removed": removed,
            "from": base.origin,
        })
    return rows


@app.get("/changes")
def changes():
    """Every note this session has changed, oldest first."""
    return _change_rows()


@app.get("/changes/diff")
def changes_diff(path: str = ""):
    """One note's diff as flat rows: `-` removed, `+` added, ` ` context, `@` gap."""
    import difflib

    from silica.kernel.write import session_changes

    base = session_changes.snapshot().get(path)
    if base is None:
        return {"path": path, "name": _clean_name(path), "kind": "unchanged", "lines": []}
    before, after = base.before or "", _read_note_text(path)
    added, removed = _tally(before, after or "")
    rows: list[dict] = []
    diff = difflib.unified_diff(before.splitlines(), (after or "").splitlines(),
                                lineterm="", n=_DIFF_CONTEXT)
    for i, ln in enumerate(diff):
        if i < 2:
            continue  # the ---/+++ file headers difflib always emits first
        if ln.startswith("@@"):
            if not rows and _HUNK_AT_TOP.match(ln):
                continue  # nothing was skipped above the first line
            rows.append({"op": "@", "text": ""})
            continue
        rows.append({"op": ln[:1] or " ", "text": ln[1:]})
    return {
        "path": path,
        "name": _clean_name(path),
        "kind": _kind(base.before, after, base.origin, bool(added or removed)),
        "from": base.origin,
        "added": added,
        "removed": removed,
        "lines": rows[:_MAX_DIFF_LINES],
        "clipped": max(0, len(rows) - _MAX_DIFF_LINES),
    }


# --- context explorer (GET /context) -----------------------------------------
# One blocking call, all of it deterministic and LLM-free. Measured on a
# 718-note vault, warm: related 0.01s, concepts(note=) 0.06s, outline/links/
# unresolved 0.00s. The first call in a fresh process pays ~0.9s to load the
# co-occurrence store — once, in a long-lived server. So: no progressive fill,
# no client-side hybrid, one endpoint that returns the whole drawer.

# Below this a note reads faster whole than as an extract, so the snippets
# section is dropped rather than duplicating the reader.
_SNIPPET_MIN_BODY = 700
_SNIPPET_CUT = 140
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
# Lines that say nothing as a one-line extract: headings, lists, quotes,
# callouts, tables, fences, images.
_NOT_PROSE = re.compile(r"^\s*(?:[-*+>#|]|\d+[.)]\s|```|~~~|!\[|\[!)")
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:\s|$)")
# A related note this far away (or unreachable) is a link that does not exist
# yet; distance 1 means it is already linked and belongs under Related instead.
_SUGGEST_MIN_DIST = 3


def _lead_prose(chunk: str) -> str:
    """The first run of plain prose in a chunk, as one line."""
    run: list[str] = []
    for line in chunk.splitlines():
        if not line.strip() or _NOT_PROSE.match(line):
            if run:
                break
            continue
        run.append(line.strip())
    return " ".join(run)


def _first_sentence(text: str, limit: int = _SNIPPET_CUT) -> str:
    """First sentence, trimmed to `limit` on a word boundary.
    ponytail: regex sentence split, so `e.g.` cuts early — a snippet, not a quote."""
    text = " ".join(text.split())
    if not text:
        return ""
    m = _SENTENCE_END.search(text)
    out = text[: m.start()] if m else text
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0] + "…"
    return out


def _key_snippets(body: str) -> list[dict]:
    """First sentence of the body plus the first sentence of each `##` section,
    at most three — a probe into the note, not a second reader."""
    if len(body) < _SNIPPET_MIN_BODY:
        return []
    parts: list[tuple[str, str]] = []
    head, last = "", 0
    for m in _H2.finditer(body):
        parts.append((head, body[last:m.start()]))
        head, last = m.group(1), m.end()
    parts.append((head, body[last:]))

    out: list[dict] = []
    for heading, chunk in parts:
        text = _first_sentence(_lead_prose(chunk))
        if text:
            out.append({"heading": heading, "text": text})
        if len(out) == 3:
            break
    return out


def _row(path: str) -> dict:
    return {"name": _clean_name(path), "path": path}


def _note_concepts(target: str, k: int = 18) -> list[dict]:
    from silica.tools.graph import silica_concepts

    try:
        return silica_concepts(note=target, k=k).get("concepts") or []
    except Exception:
        logger.debug("context: concepts failed for %s", target, exc_info=True)
        return []


def _unresolved_links() -> list:
    from silica.driver import get_driver

    try:
        return get_driver().unresolved()
    except Exception:
        logger.debug("context: unresolved() failed", exc_info=True)
        return []


def _ghost_context(name: str) -> dict:
    """An unresolved wikilink as a subject of its own.

    Today a ghost node carries path "" (graph_export.py), so clicking one used to
    post an empty path and open nothing. It has no body to read and no reader
    mode — what it does have is a name, the notes that invoke it, and their
    merged concepts, which is exactly the material for deciding whether to write
    it.
    """
    from collections import Counter

    stem = _clean_name(name).lower()
    invokers = sorted({
        link.source.path for link in _unresolved_links()
        if _clean_name(link.target).lower() == stem and link.source.path
    })
    merged: Counter = Counter()
    for src in invokers[:12]:  # ponytail: cap the fan-in; a 12-note cloud is already dense
        for c in _note_concepts(src, k=12):
            merged[c["concept"]] += c.get("weight", 1)
    return {
        "title": _clean_name(name),
        "path": "",
        "ghost": True,
        "snippets": [],
        "concepts": [{"concept": c, "weight": w} for c, w in merged.most_common(18)],
        "related": {"frontmatter": [], "outgoing": [], "backlinks": [_row(p) for p in invokers]},
        "suggested": [],
        "hint": "",
    }


def _suggested(canon: str, related: list[dict], linked: set[str], resolve) -> list[dict]:
    """How this note SHOULD be connected, in two flavours.

    - ghost: a wikilink leaving this note whose target does not exist. The note
      already claims the connection; only the file is missing.
    - note: a computed relative that scores high and sits far away (or
      unreachable) in the wikilink graph — "a missing link worth creating", per
      silica_related's own docstring. distance 1 is already linked, so it is
      Related's business, not this section's.

    Structural GAPs stay out on purpose: they are hub-to-hub by construction, so
    the section would be empty on every note that is not a hub.
    """
    out = [
        {"name": _clean_name(link.target), "path": "", "kind": "ghost",
         "why": "linked from here, never written"}
        for link in _unresolved_links()
        if link.source.path == canon
    ]
    for r in related:
        dist = r.get("distance")
        # The recall stores key on cooccur_key (path minus .md), the wikilink
        # graph on the full path — resolve back, or `linked` never matches and
        # the click target is a path the drawer cannot open.
        rpath = resolve(r["path"]) or r["path"]
        if rpath in linked or rpath == canon or (dist is not None and dist < _SUGGEST_MIN_DIST):
            continue
        out.append({
            "name": r.get("name") or _clean_name(rpath), "path": rpath, "kind": "note",
            "why": ("unreachable" if dist is None else f"{dist} hops away")
                   + f" · score {r.get('score', 0):.2f}",
        })
        if len(out) >= 8:
            break
    return out


@app.get("/context")
def context(path: str = "", name: str = "", ghost: bool = False):
    """Everything deterministic the vault knows about one note, in one call.

    Sections: key snippets (what it says), concepts (what it is about), related
    (how it IS connected), suggested (how it SHOULD be). Zero LLM calls — every
    number here is index lookup, so the drawer is a read, not a turn. Graceful
    on miss, like /note: never 500.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef
    from silica.tools.graph import silica_related

    if ghost or (not path and name):
        return _ghost_context(name or path)

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        return {"title": path, "path": path, "ghost": False, "error": "note not found in vault."}

    driver = get_driver()
    try:
        content = driver.read_note(NoteRef(name=_clean_name(canon), path=canon)).content
    except Exception:
        content = ""
    props, body = _split_frontmatter(content)

    def _refs(fn) -> list[dict]:
        # Resolved only. DRIVER.links() also returns a synthesised ref for every
        # UNRESOLVED wikilink (path "<Target>.md", a file that does not exist),
        # and listing those here would put a dead row under "how it IS
        # connected" — they belong under suggested, as links worth writing.
        try:
            return [_row(p) for r in fn(canon) if (p := resolve(r.path or ""))]
        except Exception:
            logger.debug("context: %s failed for %s", fn.__name__, canon, exc_info=True)
            return []

    outgoing = _refs(driver.links)
    backlinks = _refs(driver.backlinks)

    # frontmatter `related:` is a hand-written claim, so it is shown as written
    # and resolved only for the click target; an unresolvable entry still lists.
    fm_raw = (props or {}).get("related") or []
    if not isinstance(fm_raw, (list, tuple)):
        fm_raw = [fm_raw]
    frontmatter = [
        {"name": _clean_name(str(v)), "path": resolve(str(v)) or ""}
        for v in fm_raw if v
    ]

    try:
        rel_out = silica_related(note=canon, k=12)
    except Exception:
        logger.debug("context: related failed for %s", canon, exc_info=True)
        rel_out = {}
    rel = rel_out.get("results") or []

    linked = {r["path"] for r in outgoing} | {r["path"] for r in backlinks}
    # Whether the semantic leg contributed is READ OFF the ranking's own
    # provenance — every result names the metric that proposed it (embed:0.83,
    # cooccur:w9, edge:0.57). Asking the embed store directly would reach past
    # the relatedness facade for a fact the facade already reports.
    embed_ran = any(
        str(e).startswith("embed:") for r in rel for e in (r.get("evidence") or [])
    )

    return {
        "title": _clean_name(canon),
        "path": canon,
        "ghost": False,
        "snippets": _key_snippets(body),
        "concepts": _note_concepts(canon),
        "related": {"frontmatter": frontmatter, "outgoing": outgoing, "backlinks": backlinks},
        "suggested": _suggested(canon, rel, linked, resolve),
        # Without embeddings `related` ranks on co-occurrence alone, and the
        # section looks thin for a reason the reader cannot see from here.
        # silica_related's own hint wins when it has one — it knows more about
        # why it came back empty than an inference from the evidence can.
        "hint": rel_out.get("hint") or ("" if embed_ran else
                "no embedding index — relatedness is co-occurrence only "
                "(run /embed to add the semantic half)"),
    }


@app.get("/concept")
def concept(term: str = "", k: int = 20):
    """The notes that carry one concept — the click target of the context
    drawer's cloud, which lights them all in the graph at once. Paths are
    resolved back to graph keys so the ids match the nodes the viewer holds."""
    from silica.tools.graph import silica_concepts

    resolve = note_resolver()
    try:
        res = silica_concepts(term=term, k=k)
    except Exception as exc:
        logger.debug("concept: lookup failed for %s", term, exc_info=True)
        return {"term": term, "notes": [], "error": str(exc)}
    return {
        "term": term,
        "concept": res.get("concept") or term,
        "notes": [
            p for n in (res.get("notes") or [])
            if (p := resolve(n.get("path", "")) or n.get("path", ""))
        ],
    }


@app.get("/asset")
def asset(path: str = ""):
    """Vault-relative attachment for the note drawer, `<img>`-only by contract.
    Extension whitelist + resolved-inside-the-vault check close traversal.

    `![[img.png]]` embeds name an attachment by basename even when the file
    lives in an attachments subfolder, so an exact-path miss falls back to a
    first-match basename search under the vault (Obsidian's shortest-path rule
    minus the nearest-to-note tiebreak). rglob stays inside root, so traversal
    is still closed on the fallback path.
    ponytail: per-request rglob on the miss case; build a basename index if a
    large vault makes it slow."""
    if not path or not CONFIG.vault_path:
        raise HTTPException(status_code=404)
    if Path(path).suffix.lower() not in _ASSET_EXTS:
        raise HTTPException(status_code=404)
    root = Path(CONFIG.vault_path).resolve()
    target = (root / path).resolve()
    if not (target.is_relative_to(root) and target.is_file()):
        target = next((p for p in root.rglob(Path(path).name) if p.is_file()), None)
    if target is None or not target.is_relative_to(root) or target.suffix.lower() not in _ASSET_EXTS:
        raise HTTPException(status_code=404)
    return FileResponse(target)


@app.get("/vault_info")
def vault_info():
    """Sidebar data: vault stats + file tree, from the same builders as the
    graph view so the numbers can't disagree between the two surfaces."""
    from silica.kernel.recall.graph_export import build_graph_data, detect_communities
    from silica.ui.web.graph_view import render_tree

    try:
        nodes, edges = build_graph_data(folder="")
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        return {"error": str(exc)}
    return {
        # `path` so the header label follows a `/vault <dir>` switch mid-session:
        # this endpoint already re-runs after every turn, the boot-time header
        # never did.
        "path": CONFIG.vault_path or "",
        "notes": sum(1 for n in nodes if n.get("type") != "ghost"),
        "links": sum(1 for e in edges if e.get("type") == "EXTRACTED"),
        # STRUCTURAL clusters — Louvain on the wikilinks. The semantic partition
        # is a separate count and is not summed into this one (ADR-0023); the
        # sidebar tile says so in its tooltip, and the graph's HUD counts the
        # zones itself. Nothing here computes the semantic layer: it needs the
        # k-NN edges this endpoint has no reason to build.
        "clusters": len(communities),
        "unresolved": sum(1 for n in nodes if n.get("type") == "ghost"),
        "tree": render_tree(nodes),
        "hubs": _top_hubs(nodes, edges),
    }


def _top_hubs(nodes: list[dict], edges: list[dict], top_n: int = 24) -> list[dict]:
    """Best-connected notes by resolved-link degree — the map view's landing
    picker (a radial map must be rooted on one note, so 'most central' is the
    sensible entry point). Ghost/unlinked nodes are skipped."""
    from collections import Counter

    deg: Counter = Counter()
    for e in edges:
        if e.get("type") == "EXTRACTED":
            deg[e.get("from")] += 1
            deg[e.get("to")] += 1
    hubs = [
        {"name": n.get("label") or (n.get("path") or "").rsplit("/", 1)[-1],
         "path": n["path"], "degree": deg[n["id"]]}
        for n in nodes
        if n.get("type") != "ghost" and n.get("path") and deg[n["id"]] > 0
    ]
    hubs.sort(key=lambda h: (-h["degree"], h["name"].lower()))
    return hubs[:top_n]


@app.get("/messages")
def get_messages():
    resolve = note_resolver()
    # A call whose result carries an error must not replay as a tick: the one
    # place the user checks whether a write landed is this transcript.
    failed = {
        m["tool_call_id"] for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") and _is_tool_failure(m.get("content"))
    }
    # Tool results, which the loop below skips over: a nucleate run's outcome
    # (notes, links, which chunks died and where) exists nowhere else, so
    # without this a reloaded chat could only say the injector had run.
    results = {
        m["tool_call_id"]: m.get("content") or ""
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    data = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        tools = tool_calls_to_json(m, failed, results) if m["role"] == "assistant" else []
        content = m.get("content") or ""
        # The thinking that produced this step, kept out of the wire by _to_wire.
        # Plain text, not rendered: it is a trace, and the live block shows it raw.
        thinking = m.get("silica_reasoning") or "" if m["role"] == "assistant" else ""
        if not content and not tools and not thinking:
            continue
        data.append({"role": m["role"], "content": content, "tools": tools,
                     "thinking": thinking,
                     "html": _linkify(content, resolve) if content else ""})
    # Vault label + context usage ride headers so the body stays a plain list.
    return JSONResponse(data, headers={
        "X-Silica-Vault": CONFIG.vault_path or "",
        "X-Silica-Context-Tokens": str(CONFIG.context_tokens),
        "X-Silica-Max-Context-Tokens": str(CONFIG.max_context_tokens),
    })


@app.get("/sessions")
def list_sessions():
    # Current id rides a header so the body stays a plain list (matches /messages).
    return JSONResponse(_list_sessions(), headers={"X-Silica-Session": current_session_id or ""})


@app.post("/session/load")
def load_session(payload: dict):
    global current_session_id, _collapsed
    if _busy:
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    from silica.cli import _update_context_tokens

    sid = str(payload.get("id", ""))
    if not sid.isalnum():  # ids are uuid4 hex — blocks path traversal
        raise HTTPException(status_code=404, detail="no such session")
    path = SESSIONS_DIR / f"{sid}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no such session")
    rec = json.loads(path.read_text(encoding="utf-8"))
    messages[:] = rec.get("messages", [])
    _collapsed = set()
    current_session_id = sid
    _update_context_tokens(messages)
    return {"ok": True}


@app.post("/reset")
def reset():
    _capture_own_session()
    _reset_session()
    return {"ok": True, "vault": CONFIG.vault_path}


@app.post("/stop")
def stop():
    if current_cancel is not None:
        current_cancel.set()
    return {"ok": True}


@app.get("/health")
def health(all: bool = False):
    """The doctor's findings — non-ok by default, everything with `?all=1`.

    A server the user forgot to start degrades recall silently here: the
    embedder/reranker warnings go to the launching terminal's stderr, which the
    browser never shows. Same checks as `silica doctor`, so the two surfaces
    cannot disagree; ok rows are dropped for the sidebar notice, which is for
    what needs fixing, and kept for the settings panel's diagnostics and for a
    bug report, which needs the passing rows just as much.
    """
    from silica.onboarding.checks import run_checks

    # "session capture" tells the user to edit .claude/settings.json — a
    # Claude-Code-integration concern the browser can do nothing about. It
    # stays out of the sidebar notices; ?all=1 (diagnostics) keeps it.
    return [
        {"name": r.name, "status": r.status, "detail": r.detail, "hint": r.hint}
        for r in run_checks(CONFIG)
        if all or (r.status != "ok" and r.name != "session capture")
    ]


# 60s of 16 kHz mono 16-bit PCM is 1.92 MB; the cap is generous enough for the
# recorder's own ceiling and still refuses a body that was never a clip.
_STT_MAX_BYTES = 8 * 1024 * 1024


@app.get("/stt")
def stt_status():
    """Whether dictation can work — asked before the browser requests the mic.

    A probe, not a config flag: stt_base_url has a default, so "configured" and
    "listening" are different questions and only the second one is useful. Shares
    ensure_local_servers' readiness check, which knows that llama.cpp-family
    servers answer 503 while they load and that an open port therefore lies.
    """
    from silica.onboarding.serve import ready

    url = CONFIG.stt_base_url
    if not url:
        return {"ok": False, "url": "", "detail": "SILICA_STT_BASE_URL is empty"}
    if ready(url):
        return {"ok": True, "url": url, "detail": ""}
    return {"ok": False, "url": url, "detail": f"nothing is answering at {url}"}


@app.post("/stt")
async def stt(audio: UploadFile = File(...)):
    """Proxy one recorded clip to the transcription endpoint.

    The browser sends 16 kHz mono WAV. MediaRecorder can only produce webm/opus,
    and whisper.cpp's server reads WAV unless it was built with ffmpeg, so the
    conversion happens in app.js, where it costs no dependency on either side.
    """
    import httpx

    if not CONFIG.stt_base_url:
        raise HTTPException(503, "no transcription endpoint configured")
    clip = await audio.read()
    if not clip:
        raise HTTPException(400, "empty recording")
    if len(clip) > _STT_MAX_BYTES:
        raise HTTPException(413, f"recording over {_STT_MAX_BYTES // (1024 * 1024)} MB")
    form = {"model": CONFIG.stt_model, "response_format": "json"}
    if CONFIG.stt_lang:
        form["language"] = CONFIG.stt_lang
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{CONFIG.stt_base_url.rstrip('/')}/audio/transcriptions",
                files={"file": ("clip.wav", clip, "audio/wav")},
                data=form,
                headers={"Authorization": f"Bearer {CONFIG.stt_api_key}"},
            )
    except Exception as exc:
        raise HTTPException(502, f"transcription endpoint unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(502, f"transcription failed ({resp.status_code}): {resp.text[:200]}")
    try:
        text = (resp.json().get("text") or "").strip()
    except Exception as exc:
        raise HTTPException(502, f"transcription endpoint returned no JSON: {exc}") from exc
    return {"text": text}


@app.get("/config")
def get_config():
    """Session config for the header panel: the active model (read-only — Silica
    has no runtime model-switch op, so this mirrors the TUI's display-only
    /model) plus the one live toggle the web surfaces, thinking (/thinking)."""
    from silica.agent.providers import model_limits

    window = 0
    if CONFIG.model:
        window, _ = model_limits(CONFIG.provider, CONFIG.model)
    return {
        "model": CONFIG.model or "",
        # Empty means "the chat model does the worker's job too" (every call site
        # falls back to CONFIG.model), and the reader decides what to do with it.
        "worker_model": CONFIG.worker_model or "",
        "provider": CONFIG.provider or "",
        "context_window": window or 0,
        "show_thinking": CONFIG.show_thinking,
    }


# --- settings panel ----------------------------------------------------------
# The write half of /config is absorbed here: `thinking` is a persisted row like
# any other now, not a session-only flip. /config stays as the header chip's
# cheap read — GET /settings probes four endpoints, which is seconds the header
# label must not wait for.


@app.get("/settings")
def get_settings():
    """Every admitted row: value, where the value came from, whether it is
    locked, and its suggestions. Plus what About needs, so opening the panel is
    one round trip."""
    from silica import __version__
    from silica.onboarding.wizard import resolve_env_path
    from silica.ui.web import settings as st
    from silica.update import behind_count

    return {
        "env_path": st.short_path(resolve_env_path()),
        "busy": _busy,
        "sections": st.read_sections(),
        "version": __version__,
        "behind": behind_count(),
        "issues_url": "https://github.com/kiycoh/silica-agent/issues",
    }


def _reject_if_busy_or_locked(key: str) -> None:
    """One rule for every write: nothing lands while a turn is running.

    Deliberately not a per-row list of what a turn reads — that list would rot
    at the first new tool and no test would catch it. The cost is waiting for a
    response to finish.
    """
    from silica.ui.web import settings as st

    if _busy:
        raise HTTPException(status_code=409, detail="a response is running")
    if st.locked(key):
        raise HTTPException(status_code=409, detail=f"defined in the environment ({key})")


@app.post("/settings")
def set_setting(payload: dict):
    """Apply one row: live in CONFIG, persisted in the .env that wins at boot."""
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY or key in st.EMBED_KEYS:
        raise HTTPException(status_code=400, detail="this row goes through /settings/confirm")
    result = st.apply(key, payload.get("value", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/settings/confirm")
async def confirm_setting(payload: dict):
    """The two rows that need a sequence, not an assignment: switching the vault
    and swapping the embedding model.

    Both are long enough to block, so they run off the event loop. The embedding
    swap does not repair itself: `sweep()` decides what to re-embed from mtimes,
    which a model change never touches, so stale vectors would be compared in an
    incompatible space in silence until something forces a full re-index.
    """
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    value = str(payload.get("value", ""))
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY:
        return await asyncio.to_thread(_apply_vault_switch, value)
    if key in st.EMBED_KEYS:
        return await asyncio.to_thread(_apply_embedding_swap, key, value)
    raise HTTPException(status_code=400, detail="this row does not need confirming")


def _apply_vault_switch(path: str) -> dict:
    from silica.cli import switch_vault
    from silica.kernel.write import session_changes
    from silica.ui.web import settings as st

    switched = switch_vault(path)
    if switched.error:
        return {"ok": False, "error": switched.error}
    # The Changes list describes paths in the vault we just left.
    session_changes.clear()
    # The resolved absolute path, not what was typed: that is what the next boot
    # must read back, and what the caches were just rebuilt for.
    result = st.apply(st.VAULT_KEY, switched.vault)
    # The fresh-session seed carries the old vault's map until it is rebuilt.
    _prewarm_seed()
    notes = []
    if switched.write_dir:
        notes.append(f"writes confined to {switched.write_dir}/")
    if switched.invalid_write_dir:
        notes.append("vault.yaml declares an invalid write_dir — every write will be rejected")
    if switched.repo_warning:
        notes.append(switched.repo_warning)
    if switched.language_drift:
        notes.append(
            f"language {switched.language}, co-occurrence store frozen "
            f"{switched.store_language} — rebuild it with /cooccur --force"
        )
    return {**result, "vault": switched.vault, "notes": notes}


def _apply_embedding_swap(key: str, value: str) -> dict:
    from silica.tools import TOOLS
    from silica.ui.web import settings as st

    result = st.apply(key, value)
    if not result["ok"]:
        return result
    raw = TOOLS["silica_embed_refresh"].run(folder="", force=True)
    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        report = {"result": str(raw)[:200]}
    return {**result, "reindex": report}


@app.get("/bug_report")
def get_bug_report():
    """The diagnostic block a bug report attaches. Built server-side on purpose:
    in the browser the API keys sit in the panel's own fields, and a public issue
    is exactly the wrong place for one."""
    from silica.ui.web import settings as st

    return st.bug_report()


@app.get("/endpoints")
def get_endpoints():
    from silica.ui.web import settings as st

    return st.endpoint_status()


@app.post("/endpoints/start")
async def start_endpoint(payload: dict):
    """Start one local endpoint from the command its own .env key names. Loading
    a model takes tens of seconds, so this waits on a worker thread."""
    from silica.ui.web import settings as st

    return await asyncio.to_thread(st.start_endpoint, str(payload.get("label", "")))


@app.get("/")
def index():
    # Cache-bust app.js/app.css by content hash: StaticFiles sets no
    # Cache-Control, so browsers serve them stale from heuristic freshness
    # (edited JS never reaches the page). A content-keyed URL can't be stale.
    # The big vendored bundles keep their long-lived cache — only these churn.
    import hashlib

    from silica.config import CONFIG

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "app.css"):
        ver = hashlib.sha256((STATIC_DIR / asset).read_bytes()).hexdigest()[:8]
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    # The preference, not the resolution: "auto" is a question only the browser
    # can answer, and the inline script in <head> answers it before first paint.
    # Stamping it server-side is what keeps a light session from flashing dark.
    pref = CONFIG.theme if CONFIG.theme in ("auto", "dark", "light") else "auto"
    html = html.replace('data-theme-pref="auto"', f'data-theme-pref="{pref}"')
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def serve(port: int = 8765) -> None:
    """Apply config, open the browser on startup, then block on uvicorn."""
    import uvicorn

    from silica.ui.banner import print_banner
    from silica.ui.console import CONSOLE

    _reset_session()

    print_banner()
    CONSOLE.print(f"  [dim]GUI live at[/] [cyan]http://127.0.0.1:{port}[/]\n")

    try:
        uvicorn.run(app, host="127.0.0.1", port=port)
    finally:
        _capture_own_session()  # last chance: this conversation ends with the server
