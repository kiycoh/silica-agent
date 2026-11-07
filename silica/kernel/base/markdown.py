# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import re
import textwrap
from markdown_it import MarkdownIt

from silica.kernel.base.formats import TABULAR_EXTS, VISUAL_MEDIA_EXTS
from silica.kernel.base.frontmatter import FM_RE_SCAN
from silica.kernel.base.syntax import ADR_REF_RE

def lf(text: str) -> str:
    """`text` on LF, as Python's universal newlines read it: the one input
    every composer is written for (ADR-0093 decision 8)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def crlf_dominant(raw: bytes) -> bool:
    """CRLF when the bytes hold more `\\r\\n` than lone `\\n` (decision 8)."""
    crlf = raw.count(b"\r\n")
    return crlf > raw.count(b"\n") - crlf


def encode(content: str, crlf: bool) -> str:
    """`content` (composed on LF) with the prior's terminator put back."""
    content = lf(content)
    return content.replace("\n", "\r\n") if crlf else content


_MD = MarkdownIt()  # stateless parser, shared by every parse below
_FOOTNOTE_DEF_RE = re.compile(r"^([ \t]{0,3})\[\^[^\]\n]+\]:[ \t]?", re.MULTILINE)

# One list, two jobs: extract_links DROPS these targets (a file is not a note;
# before .ipynb/.mov/.tiff joined 2026-08-27, each such link sat in the
# backends' unresolved set as a phantom dangling NOTE) and extract_file_refs
# RETURNS them as the note→file reference bucket. Deliberately a superset of
# VISUAL_MEDIA_EXTS: that list is "what to strip from prose" (visual media only —
# a .zip or .ipynb link must stay in the text), this one is "what is a file".
# Extension allowlist, not "has a dot": note titles like "Node.js" or "Web 2.0"
# must stay note targets.
# Tabular data: a profile note names its source file, and as note targets these
# were the same phantom dangling links the 2026-08-27 batch fixed for notebooks.
NON_MD_EXTENSIONS = (*VISUAL_MEDIA_EXTS, '.zip', '.html', '.css', '.ipynb', *TABULAR_EXTS)

# Every marker that can make `_extract_links_ast` return anything. It reads
# targets from two places only — a text token containing `[[…]]`, and the
# `[[href]]` it injects for a link_open/image token — and each of those needs
# one of these six shapes in the source:
#
#   [[                       a literal wikilink
#   ](  not web              an inline destination that survives the
#                            http/https/mailto filter in the walk
#   [label]:                 a link reference definition, which turns a bare
#                            [label] elsewhere into a link with no `](` in
#                            the source at all
#   \[                       a backslash escape decoding to '['
#   &#91; &#x5B;             an entity decoding to '['
#   &lbrack; &lsqb;
#   <scheme:  not web        an autolink whose href the same filter keeps
#
# Nothing else in CommonMark puts a '[' into a text token or an href into the
# walk, so no marker means no link and the parse can be skipped. The web
# exclusions are case-sensitive on purpose: so is the filter they model, which
# is why `[x](HTTP://y)` really does yield a target today.
#
# Worth the care because this is the hot loop of every index rebuild: measured
# 2026-08-24, `extract_links` was 3.65s of build_graph_data's 3.86s over 395
# notes, all of it markdown-it, and the rebuild is per-process — every
# invocation pays it again. With the scan: 0.80s, and 58% of extract_links'
# wall clock gone across 2500 files / 23.3 MB of real vaults, with zero
# disagreements against the parser over the same corpus.
#
# The two directions are not symmetric. A marker too many costs one parse; a
# marker too few drops a link, which reads downstream as a *removed* edge and
# rolls the chunk back at the graph gate. So anything unproven belongs on the
# parser side of this scan, not this side — and every alternative here is
# mutation-tested (tests/test_wikilink_fast_path.py), because a scan that is
# merely nearly right looks exactly like one that is right.
_LINK_MARKER_RE = re.compile(
    r"\[\["
    r"|ADR-\d{4}\b"
    r"|\]\((?!https?://|mailto:)"
    # Unanchored on purpose. A definition is document-global wherever it sits,
    # and inside a blockquote its line starts with '>', not whitespace — the
    # line-anchored version of this alternative returned [] for
    # "> [b]: Target.md" while the parser returned the target (found by
    # mutation-testing the scan, 2026-08-24). Matching a stray "[x]:" in prose
    # costs one parse; missing one costs an edge.
    r"|\[[^\]\n]*\]:"
    r"|\\\["
    r"|&(?:#0*91|#[xX]0*5[bB]|lbrack|lsqb);"
    # The lookbehinds carry the '<' so a scheme merely *ending* in a web one
    # (<xhttp://…>, a real target) is not mistaken for the web scheme itself.
    r"|<[A-Za-z][A-Za-z0-9+.\-]{1,31}:(?<!<http:)(?<!<https:)(?<!<mailto:)"
)

# The mention fast path. Backtick cites carry none of the link markers above,
# and widening _LINK_MARKER_RE to any backtick would send nearly every note to
# the parser (prose is full of `code`). Anchoring the scan on the censused
# extensions keeps the fast path honest: only a note that visibly carries a
# path-shaped inline cite pays a parse. Field test 2026-08-28: 163/175 CSV
# citations in a real vault were inline code, 0 were wikilinks.
_CODE_MENTION_RE = re.compile(
    r"`[^`\s]+\.(?:" + "|".join(re.escape(e[1:]) for e in NON_MD_EXTENSIONS) + r")`",
    re.IGNORECASE,
)


_OPEN_ALIAS_RE = re.compile(r"\[\[[^\[\]\n]*\|[^\[\]\n]*$")
_ALIAS_END_RE = re.compile(r"[^\[\]\n]*\]\]")


def join_code_aliases(children: list) -> list:
    """Inline tokens with a code span in a wikilink's alias back in one text token.

    `[[Undo and revert|`/undo`]]` parses as text, code_inline, text, so neither
    half is a link: the graph lost the edge and /note printed the brackets, on 6
    of silica_wiki's notes (2026-11-06), while the drawer's editor, which parses
    for itself, drew the link. On tokens and not on the source, so a fence or a
    whole code span showing the same characters stays code.
    """
    # shortcut: one code span per alias, the only form in those 6 notes, so
    # `[[A|`x` and `y`]]` still splits; walk on to the closing ]] the day a vault
    # is found holding one.
    out: list = []
    i = 0
    while i < len(children):
        tok = children[i]
        after = children[i + 1] if i + 1 < len(children) else None
        if (tok.type == "code_inline" and out and out[-1].type == "text" and after is not None
                and after.type == "text" and _OPEN_ALIAS_RE.search(out[-1].content)
                and _ALIAS_END_RE.match(after.content)):
            out[-1].content += tok.content + after.content
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def extract_links(content: str) -> list[str]:
    """Extract clean wikilinks (both [[target]] and ![[target]]).

    Answers from a marker scan when that scan can prove the CommonMark parse
    would have found nothing, and from the parse itself otherwise. The two are
    the same function to every caller; see `_LINK_MARKER_RE` for why they have
    to be, and what happens if they ever stop being.
    """
    # Scanned after dedent so there is only ever one string in play — the one
    # the parser would have seen. No marker is anchored to a column, so the
    # order is not load-bearing (verified over 564 real notes: dedent flips no
    # verdict); keeping it this way just means a reader never has to check.
    content = textwrap.dedent(content)
    if not _LINK_MARKER_RE.search(content):
        return []
    return _extract_links_ast(content)


def extract_file_refs(content: str) -> list[str]:
    """Non-md file targets a note references: ![[img.jpg]] embeds, [[doc.pdf]]
    wikilinks, ![alt](x.png) images and [label](x.ipynb) links, in order,
    deduped. Remote (http/https/mailto) sources never appear — the walk in
    `_extract_refs_ast` filters them before the buckets exist.

    Same marker-scan fast path as extract_links, and provably so: every file
    ref carries one of its markers (`![[` contains `[[`; `![](x.png)` and
    `[l](x.pdf)` contain a non-web `](`), so a scan-negative note has no file
    refs either.
    """
    content = textwrap.dedent(content)
    if not _LINK_MARKER_RE.search(content):
        return []
    return _extract_refs_ast(content)[1]


def _extract_links_ast(content: str) -> list[str]:
    """`extract_links` without the marker scan, on already-dedented content.

    Split out so the equivalence between the two paths is a thing the tests
    can assert directly (tests/test_wikilink_fast_path.py) instead of a claim.
    """
    return _extract_refs_ast(content)[0]


def _extract_refs_ast(content: str) -> tuple[list[str], list[str], list[str]]:
    """One parse, three buckets: (note targets, file targets, code mentions).

    The note/file split lives at the extension check that used to silently
    drop file targets — everything upstream (walk, filters, normalization) is
    shared, so the two buckets can never disagree about what counts as a
    reference (ADR-0029: one link generator). Mentions are the third bucket,
    not a second generator: same parse, same walk, and the resolver downstream
    is the same asset census — but a mention is a *casual* cite, so a miss is
    dropped where a wikilink miss is reported. This is not the mention index
    ADR-0029 deleted: that one fuzzy-matched note TITLES in prose; this
    resolves literal path tokens.
    """
    # A footnote definition is text, not a link reference definition. Parsed as
    # CommonMark, `[^L]: [[sources/X]]` (how CLEANUP cites a source leaf) was a
    # reference whose target was the literal `[[sources/X]]`, so every cited
    # note's edge to its leaf was lost (ADR-0055). Dropping the label keeps the
    # line's own text. A regex and not mdit_py_plugins' footnote plugin, which
    # ships only with the [gui] extra while this parser runs everywhere.
    content = _FOOTNOTE_DEF_RE.sub(r"\1", content)
    tokens = _MD.parse(content)

    text_pieces: list[str] = []
    mentions: list[str] = []

    def walk(toks: list) -> None:
        for t in toks:
            if t.type == "inline":
                if t.children:
                    walk(join_code_aliases(t.children))
            elif t.type == "text":
                text_pieces.append(t.content)
            elif t.type == "code_inline":
                # `data/raw/x.csv` in prose. Single path-shaped token only: a
                # snippet with spaces is code, a token with a scheme is a URL,
                # and only censused extensions can ever resolve. Fenced blocks
                # never reach here — they are `fence` tokens, not code_inline.
                c = t.content.strip()
                if (c and not re.search(r"\s", c) and "://" not in c
                        and c.lower().endswith(NON_MD_EXTENSIONS)
                        and c not in mentions):
                    mentions.append(c)
            elif t.type == "image":
                src = t.attrs.get("src")
                if src and not (src.startswith("http://") or src.startswith("https://") or src.startswith("mailto:")):
                    text_pieces.append(f"[[{src}]]")
            elif t.type == "link_open":
                href = t.attrs.get("href")
                if href and not (href.startswith("http://") or href.startswith("https://") or href.startswith("mailto:") or href.startswith("#")):
                    text_pieces.append(f"[[{href}]]")

    walk(tokens)

    cleaned: list[str] = []
    files: list[str] = []
    for text in text_pieces:
        # Match [[target]] links (allowing characters like # and ^)
        raw_targets = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', text)
        for t in raw_targets:
            t = t.strip().replace("''", "'")
            if not t:
                continue
            # [[#Heading]] / [[^block]] point INSIDE the note that carries
            # them: they are not note links, and no resolver downstream can
            # ever match one, so each landed in the backends' unresolved set
            # and read to the graph gate as a newly introduced dangling link —
            # 2 of the 6 chunk rollbacks in the 2026-08-21 machine_learning run
            # were an intra-note anchor. link/sweep.py's regex already refuses
            # to match them; this is the outlier that did not.
            if t.startswith('#') or t.startswith('^'):
                continue
            # Split off heading/section part. For a file target this also
            # drops an Obsidian sub-reference ([[doc.pdf#page=3]] → doc.pdf).
            t = t.split('#', 1)[0].strip()
            if not t:
                continue
            if t.lower().endswith(NON_MD_EXTENSIONS):
                if t not in files:
                    files.append(t)
                continue
            if t not in cleaned:
                cleaned.append(t)
        # Prose only: `text` tokens. A fenced or inline-code `ADR-0001` is a
        # `fence`/`code_inline` token and never reaches text_pieces.
        for num in ADR_REF_RE.findall(text):
            if f"ADR-{num}" not in cleaned:
                cleaned.append(f"ADR-{num}")
    return cleaned, files, mentions


_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t].*$", re.MULTILINE)


def extract_links_typed(content: str) -> dict[str, bool]:
    """`extract_links` targets, each flagged scaffold (True) or prose (False).

    Scaffold is a link the vault's structure wrote rather than its prose: a
    frontmatter property (`parent note`, `related:`) or a heading line
    (`## [[Concept]]`, a hub's spoke list). Measured 2026-08-23 on a 709-note
    human vault: 257 of 1059 linked pairs exist only as scaffold (24%). The
    flag rides on the graph edge so a consumer can tell the two apart; none
    switches by default, because on that vault Adamic-Adar scored AUC 0.806
    with scaffold and 0.750 without, and dropping it split the graph from 96
    components into 178 (ADR-0029). A target that occurs in prose anywhere is
    prose, whatever else mentions it. Same targets, same order as
    `extract_links`.
    """
    return extract_refs_typed(content)[0]


def extract_refs_typed(content: str) -> tuple[dict[str, bool], list[str], list[str]]:
    """`extract_links_typed` plus the file and mention buckets, one parse.

    The per-note call for the backends' index pass: separate calls
    (typed + extract_file_refs) would parse every link-bearing note twice
    for nothing — the parse is the hot loop's measured cost. The mention scan
    widens the fast path only for notes carrying a path-shaped inline cite,
    so link-free prose still skips the parser.
    """
    content = textwrap.dedent(content)
    if not (_LINK_MARKER_RE.search(content) or _CODE_MENTION_RE.search(content)):
        return {}, [], []
    all_targets, files, mentions = _extract_refs_ast(content)
    if not all_targets:
        return {}, files, mentions
    m = FM_RE_SCAN.match(content)
    body = content[m.end():] if m else content
    prose = set(extract_links(_HEADING_LINE_RE.sub("", body)))
    return {t: t not in prose for t in all_targets}, files, mentions


def parse_headings(body: str) -> list[dict]:
    """Parse headings from the body using AST, ignoring code blocks."""
    body = textwrap.dedent(body)

    # YAML frontmatter is no heading, but its closing `---` is a setext
    # underline to CommonMark: every note with properties opened its outline
    # with them as an H2 (silica_outline, found 2026-10-20). Blanked, not cut,
    # so every `pos` below stays an offset into `body`.
    m = FM_RE_SCAN.match(body)
    tokens = _MD.parse(re.sub(r"[^\r\n]", " ", m.group(0)) + body[m.end():] if m else body)

    lines = body.splitlines(keepends=True)
    line_offsets = []
    current_offset = 0
    for line in lines:
        line_offsets.append(current_offset)
        current_offset += len(line)

    headings = []
    for idx, t in enumerate(tokens):
        if t.type == "heading_open":
            level = int(t.tag[1])
            # Find next inline token to get heading text
            next_t = tokens[idx + 1]
            text = next_t.content
            
            line_idx = t.map[0] if t.map else 0
            pos = line_offsets[line_idx] if line_idx < len(line_offsets) else len(body)
            headings.append({"level": level, "text": text, "pos": pos})

    return headings


def _balanced(body: str) -> list[str]:
    """Check for unbalanced OFM structural delimiters (fence-aware) using AST."""
    body = textwrap.dedent(body)
    issues = []
    # If there's an odd number of ``` in the raw body, code fence is unclosed
    if body.count("```") % 2:
        issues.append("unclosed code fence")


    tokens = _MD.parse(body)

    text_pieces: list[str] = []

    def walk(toks: list) -> None:
        for t in toks:
            if t.type == "inline":
                if t.children:
                    walk(t.children)
            elif t.type == "text":
                text_pieces.append(t.content)

    walk(tokens)

    # Joined with a separator: glued pieces let two list items ending and
    # starting with `$` read as one `$$` (2026-09-05, two notes deferred at
    # WRITE on a false "unbalanced $$ block").
    combined_text = "\n".join(text_pieces)

    if combined_text.count("$$") % 2:
        issues.append("unbalanced $$ block")
    if combined_text.count("==") % 2:
        issues.append("unbalanced == highlight")
    if combined_text.count("[[") != combined_text.count("]]"):
        issues.append("unbalanced [[wikilink]]")

    return issues


def extract_callouts(body: str) -> list[str]:
    """Extract Obsidian callout types (e.g. 'note', 'tip') from blockquotes."""
    body = textwrap.dedent(body)

    tokens = _MD.parse(body)

    callout_types = []
    for idx, t in enumerate(tokens):
        if t.type == "blockquote_open":
            # Search for the first inline token inside the blockquote
            for k in range(idx + 1, len(tokens)):
                if tokens[k].type == "blockquote_close":
                    break
                if tokens[k].type == "inline":
                    content = tokens[k].content
                    match = re.match(r'^\[!([A-Za-z]+)\]', content)
                    if match:
                        callout_types.append(match.group(1))
                    break
    return callout_types


def get_non_code_text(body: str) -> str:
    """Extract all text tokens from body, ignoring code blocks/fences/inline-code."""
    body = textwrap.dedent(body)

    tokens = _MD.parse(body)
    text_pieces = []
    def walk(toks: list) -> None:
        for t in toks:
            if t.type == "inline":
                if t.children:
                    walk(t.children)
            elif t.type == "text":
                text_pieces.append(t.content)
    walk(tokens)
    return "".join(text_pieces)


# One alternation, because the first opener wins: run as two subs, the %% inside
# `<!-- 5%% -->` paired with a %% paragraphs below and took the note between them.
# `<!-->` and `<!--->` are whole comments to markdown-it. `open` is one never closed:
# markdown-it's html_block runs to the end, and Obsidian hides a lone %% the same way.
_HIDDEN = re.compile(r"%%.*?%%|<!--(?:-?>|.*?-->)|(?P<open>%%|<!--).*\Z", re.S)
_BLOCK_ID = re.compile(r"[ \t]+\^[\w-]+[ \t]*$", re.M)
# Inline code span: a backtick run closed by an equal-length run, in one paragraph as
# markdown-it reads it: across a blank line, two stray backticks parked the remark
# between them. Parked before the %%/^ subs so span contents read as code, not markup.
_CODE_SPAN = re.compile(r"(`+)(?:(?!\n[ \t]*\n)[\s\S])*?\1")
_PARKED = re.compile(r"\x00(\d+)\x00")


def _code_lines(text: str) -> set[int]:
    """The lines markdown-it reads as code: a fence at any depth, in a quote or a list, and
    an indented block, which no pattern on one line can tell from a list's continuation."""
    return {n for tok in _MD.parse(text) if tok.type in ("fence", "code_block") and tok.map
            for n in range(*tok.map)}


# Inline spans are parked as \x00 sentinels for the length of the sub, the
# same trick mdLite plays client-side; pathological nested backtick runs can
# still fool the span regex, which costs a stripped %% inside them, cosmetic.
def strip_ofm_meta(text: str, published: bool = False) -> str:
    """Strip %%comments%% and trailing ^block-ids, sparing code, where %% and ^
    are code (a lone %% in a fence would otherwise pair with a prose %% and
    swallow everything between). A comment already open beats the code below
    it: an author comments a block out by wrapping it, and a fence decided
    first let that block out.
    `published` is for text that leaves as Markdown, where nothing else hides
    a comment: an HTML one, which the renderer's allowlist drops from a page
    alone, and a %% never closed. A drawer keeps both, since a chat answer may
    say "100%% sure". The check before Publish reads through here too, so what
    it reads is what goes out: its own pattern took a diagram's %% comment for
    a remark to the end of the note, and an address below it was never read."""
    if "%%" not in text and "<!--" not in text and not _BLOCK_ID.search(text):
        return text
    code = _code_lines(text)
    pieces: list[str] = []
    run: list[str] = []

    def _hide(m: "re.Match[str]") -> str:
        closed_remark = m.group(0).startswith("%%") and not m["open"]
        return "" if published or closed_remark else m.group(0)

    def _comment_open() -> bool:
        return any(m["open"] for m in _HIDDEN.finditer(_CODE_SPAN.sub("", "\n".join(run))))

    def _flush() -> None:
        if run:
            spans: list[str] = []

            def _park(m: "re.Match[str]") -> str:
                spans.append(m.group(0))
                return f"\x00{len(spans) - 1}\x00"

            parked = _CODE_SPAN.sub(_park, "\n".join(run))
            cleaned = _BLOCK_ID.sub("", _HIDDEN.sub(_hide, parked))
            pieces.append(
                _PARKED.sub(lambda m: spans[int(m.group(1))], cleaned))
            run.clear()

    for i, line in enumerate(text.split("\n")):
        if i in code and not _comment_open():
            _flush()
            pieces.append(line)
        else:
            run.append(line)
    _flush()
    return "\n".join(pieces)


def resolve_relative(target: str, source_path: str) -> str | None:
    """Vault-rooted form of a `./`/`../` link target, joined against the source
    note's folder; None when the walk escapes the vault root (no note can match,
    and a stripped guess would resolve to the wrong file).

    Any other target returns unchanged: bare names and vault-rooted paths
    already resolve in both backends, and rewriting them here would change
    measured resolution behavior for no defect. Resolution-side only — the one
    link GENERATOR stays extract_links (ADR-0029); before this helper existed
    the suffix matcher saw "/./x.md", matched nothing, and one INDEX.md carried
    332 ghost targets (2026-08-25).
    """
    if not (target.startswith(("./", "../")) or "/./" in target or "/../" in target):
        return target
    import posixpath

    base = source_path.rsplit("/", 1)[0] if "/" in source_path else ""
    joined = posixpath.normpath(posixpath.join(base, target))
    if joined == ".." or joined.startswith("../"):
        return None
    return joined


def has_wikilink(content, name):
    # Obsidian resolves links case-insensitively and with alias/heading
    # suffixes: [[machine learning]], [[Machine Learning|ML]] and
    # [[Machine learning#Storia]] all satisfy hub "Machine learning".
    c, n = content.casefold(), name.casefold()
    return any(f"[[{n}{suffix}" in c for suffix in ("]]", "|", "#"))
