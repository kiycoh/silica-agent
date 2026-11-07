# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Link cases both web renders must agree on.

An answer is rendered twice: `_linkify` (markdown-it, server, canonical) and
`mdLite` (hand-rolled JS, the live streaming segment). Two languages, one
contract — so the corpus lives here and both halves assert the same list. A
drift where one side gains a behavior the other lacks already has its case
written on both sides.

Only the URL half is shared: note refs resolve server-side against the vault
index and are left to the caller in the live segment, so those assertions stay
in their own test.
"""

# (markdown, must appear, must NOT appear)
URL_CASES: list[tuple[str, list[str], list[str]]] = [
    # A citation line as _sources_block writes it: bare URL, and the URL text
    # survives so a terminal or a reader without link support still reads it.
    (
        "1. Chem - https://en.wikipedia.org/wiki/chemistry",
        ['<a href="https://en.wikipedia.org/wiki/chemistry">'
         "https://en.wikipedia.org/wiki/chemistry</a>"],
        [],
    ),
    # Sentence punctuation belongs to the prose, not to the href.
    ("vedi https://ex.com/a. ok", ['<a href="https://ex.com/a">https://ex.com/a</a>.'], []),
    # …but a URL's own balanced parens do not (Wikipedia disambiguation).
    (
        "(https://en.wikipedia.org/wiki/A_(b))",
        ['href="https://en.wikipedia.org/wiki/A_(b)"'],
        [],
    ),
    # Code is never linkified, in either render.
    ("run `https://x.org/a` inline", ["<code>https://x.org/a</code>"], ["<a href"]),
    # fuzzy_link off: `.md` is a real ccTLD, and a vault path is not a website.
    # fuzzy_email off: prose must not open a mail client.
    (
        "vedi nota.md e www.x.org e foo@bar.com",
        [],
        ["<a href", "http://nota.md", "mailto:"],
    ),
]


# Note refs, as a render that resolves them to themselves draws them. mdLite passes a
# target through raw and /note resolves it on click, so with an identity resolver the
# two must print the same element. (markdown, must appear in both)
NOTE_CASES: list[tuple[str, list[str]]] = [
    ("see [[Note]] here", ['<a class="note-link" data-path="Note">Note</a>']),
    ("see [[Note|the alias]] here", ['<a class="note-link" data-path="Note">the alias</a>']),
    ("see [[Folder/Note]] here", ['<a class="note-link" data-path="Folder/Note">Note</a>']),
    ("see [[Note#Heading|h]] here", ['<a class="note-link" data-path="Note">h</a>']),
    ("![[fig.png]]", ['<img src="/asset?path=fig.png" alt="fig">']),
    ("![[fig.png|300]]", ['<img src="/asset?path=fig.png" alt="fig" width="300">']),
    ("![[fig.png|a chart]]", ['<img src="/asset?path=fig.png" alt="a chart">']),
    ("![[my fig.png]]", ['<img src="/asset?path=my%20fig.png" alt="my fig">']),
    ("```mermaid\ngraph TD\n```\n", ['<pre class="mermaid">graph TD']),
    ("| a | b |\n|---|:-:|\n| 1 | 2 |\n", ["<table>", "<th>a</th>", '<th style="text-align:center">b</th>',
                                          "<td>1</td>", '<td style="text-align:center">2</td>']),
    ("# Head\n", [">Head</h1>"]),
    ("**bold** and *it* and `code`", ["<strong>bold</strong>", "<em>it</em>", "<code>code</code>"]),
]

# What the editor's Lezer grammar and the renderer must read alike, the pattern first:
# the editor draws these as you type and the reader draws them on save.
# (text, is it inline math)
MATH_CASES: list[tuple[str, bool]] = [
    ("$x^2$", True),
    ("$a+b$ then", True),
    ("$5 and $10", False),     # allow_digits=False: a price is not a formula
    ("$ x$", False),           # allow_space=False: not after the opening `$`
    ("$x $", False),           # nor before the closing one
    ("$x$5", False),
]
# (text, the tag or None)
TAG_CASES: list[tuple[str, str | None]] = [
    ("#tag", "tag"),
    ("#a/b-c_d", "a/b-c_d"),
    ("#123", None),            # a number is not a tag
    ("#città", "città"),
]
# The one known difference, stated so it can be seen: the editor takes Obsidian's rule, at
# least one non-digit anywhere and any letter first; the reader has only ever taken an ASCII
# letter or `_` first. `#1a` and `#è` are drawn as tags while typing and read as words after.
TAG_DRIFT: list[str] = ["#1a", "#èlite"]
