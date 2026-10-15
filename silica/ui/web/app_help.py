# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the chat knows about the app it runs in (ADR-0115).

The help panel in `static/index.html` is the source: this reads its rows as the
surface renders them, so a row fixed for the reader is fixed for the chat in
the same edit. A paragraph about the app written in the prompt would be a second
copy, and the panel itself held two wrong rows until ADR-0111 rewrote them.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).parent / "static" / "index.html"

# A view's address, which the hash router opens (app-shell.js tabFromHash).
ADDRESS = {"Explore": "#explore", "Learn": "#learn", "Calendar": "#calendar", "Examine": "#examine"}

# Not "point to the help panel" for what it does not name: it holds these same
# rows, so the pointer led nowhere, and the first wording's answers went on to
# guess what the help would show ("there you should find the export section",
# probe of 2026-10-16).
# A view is written as a link because a bare "#calendar" in an answer renders as
# a tag, not as the view (seen in the app on 2026-10-16), and the model copies
# the form the rows use. It also links #settings or #new, which open nothing
# (7 of 55 links, 2026-10-16); telling it that only views have addresses left
# 8 of 56, so the sentence was taken out again.
LIMIT = (
    "These rows are everything the app documents about its screens; its help panel "
    "(the ? key) shows the person these same rows, and its Guided tour button walks a "
    "first visit through the app. For a control, screen or setting the rows do not "
    "name, say you do not know of one in the app. Do not guess where it might be, and "
    "do not send the person to look for it in the help or the settings. What you can "
    "do yourself with your own tools stays yours to offer. You cannot press anything "
    "in the app: say where to click, never that you clicked. Name a view as the rows "
    "do, as a link like [Learn](#learn), which opens it; a bare #learn shows as a tag."
)


class _Panel(HTMLParser):
    """Sections of `#help-panel`: an `sp-title` opens one, each `hp-row` is a
    row whose last direct child is the text and the children before it the term."""

    def __init__(self, hosted: bool):
        super().__init__()
        self.hosted = hosted
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []
        self.depth = 0            # depth inside #help-panel, 0 when outside
        self.title: list[str] | None = None
        self.row: list[list[str]] | None = None
        self.row_depth = 0
        self.skip = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth == 0:
            if a.get("id") == "help-panel":
                self.depth = 1
            return
        if tag not in ("br", "img", "input"):
            self.depth += 1
        cls = (a.get("class") or "").split()
        if "sp-title" in cls:
            self.title = []
        elif "hp-row" in cls:
            self.row, self.row_depth = [], self.depth
            self.skip = self.hosted and a.get("data-surface") == "local"
        elif self.row is not None and self.depth == self.row_depth + 1:
            self.row.append([])

    def handle_endtag(self, tag):
        if self.depth == 0 or tag in ("br", "img", "input"):
            return
        if self.title is not None:
            self.sections.append(("".join(self.title).strip(), []))
            self.title = None
        elif self.row is not None and self.depth == self.row_depth:
            parts = [" ".join("".join(p).split()) for p in self.row if "".join(p).strip()]
            if parts and self.sections and not self.skip:
                self.sections[-1][1].append((" ".join(parts[:-1]), parts[-1]))
            self.row = None
        self.depth -= 1

    def handle_data(self, data):
        if self.title is not None:
            self.title.append(data)
        elif self.row is not None and self.row:
            self.row[-1].append(data)


def panel(hosted: bool) -> list[tuple[str, list[tuple[str, str]]]]:
    p = _Panel(hosted)
    p.feed(INDEX.read_text(encoding="utf-8"))
    return [s for s in p.sections if s[1]]


def app_message() -> str:
    from silica.config import hosted as _hosted
    from silica.ui.web.settings import sections

    hosted = _hosted()
    where = ("hosted at app.silica-agent.dev" if hosted
             else "running on this person's own machine")
    lines = ["## The app you are in",
             f"You are the chat of the Silica web app, {where}. What follows is its "
             "help panel as this person sees it."]
    for title, rows in panel(hosted):
        lines += ["", f"### {title}"]
        for term, text in rows:
            addr = ADDRESS.get(term)
            lines.append(f"- [{term}]({addr}): {text}" if addr else f"- {term}: {text}")
    lines += ["", "### settings (ctrl ,)"]
    # Labels only: a row's value can be a key.
    lines += [f"- {name}: " + ", ".join(r.label for r in rows)
              for name, rows in sections().items() if rows]
    return "\n".join(lines + ["", LIMIT])
