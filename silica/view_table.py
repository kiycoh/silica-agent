# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a view is, one row each, and what a demo refuses beyond its views. A view's
commands, and a demo's, are columns of `command_table`.

A table and nothing else, as `provider_table` is, because config reads it to say
what a process serves: a route that moves edits this file and not the module
everything imports. `index.html`'s `data-tab` and `data-view` markers are pinned
to these rows by tests/test_served_views.py.
"""
from __future__ import annotations

from typing import NamedTuple


class View(NamedTuple):
    """A view beyond the chat, by the tab id the page gives it (ADR-0140 point 4):
    one a process does not serve loses its routes, its chat tools and its commands
    (`Command.view`) with its tab. A route is its path or anything under it."""
    slug: str   # its address, #slug: what the hash router opens and the chat writes
    label: str  # what the help panel's row calls it
    routes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    # Whether the chat is told the address. My Wikis came after the rows learned
    # to be links and was never given one, and a line of the chat's prompt
    # changes on a decision and not as a side effect of this table.
    linked: bool = True


# Explore has no tools of its own: the chat reads the notes it draws.
VIEWS: dict[str, View] = {
    "discover": View("discover", "Discover", routes=("/discover",)),
    "graph": View("explore", "Explore", routes=("/graph", "/map/layout", "/compare", "/shape")),
    "learn": View("memorize", "Memorize", routes=("/learn",),
                  tools=("silica_learn_items", "silica_review_queue", "silica_record_quiz")),
    "calendar": View("calendar", "Calendar", routes=("/calendar", "/reminders"),
                     tools=("silica_agenda", "silica_event_create", "silica_event_update")),
    "metrics": View("examine", "Examine", routes=("/metrics",)),
    # ADR-0143: the choice and Publish; saving a page (/vault/save) is every account's
    "public": View("public", "My Wikis", routes=("/public",), linked=False),
}


# What a demo refuses beyond its views (ADR-0139 point 6), at the doors a view
# already has: a route answers 404 as one that does not exist. A command is
# neither offered nor run (`Command.demo_off`). The chat's tools are
# constraints.DEMO_WITHHELD.
DEMO_OFF: dict[str, tuple[str, ...]] = {
    "routes": (
        # an upload is personal data, and an import reaches the internet
        "/chat/files/add", "/vault/import", "/vault/fetch", "/vault/save", "/mail/upload", "/note/asset",
        # Settings and what only Settings reaches. The Caddy block that refuses
        # /settings/*, /endpoints, /zotero and /bug_report on a tenant's route
        # never sees the demo's hostname (ADR-0139 point 4).
        "/settings", "/connectors", "/skills", "/instructions", "/endpoints", "/zotero", "/bug_report",
        # dictation sends the voice outside the count; a flag would die unread
        "/stt", "/flag",
        # model calls the door never counts: a turn outside POST /chat, the
        # injector, check sources' judge, the worker's sentence on the vault
        "/turn/redo", "/phone", "/nucleate", "/note/check", "/note/check/keep", "/vault_brief",
        # a job's turn (POST /jobs/{id}/turn) is a whole agent loop the door
        # never counts, whatever queued the job; with no upload there is none
        # to list, and the tray leaves itself as it was on a 404 (jobsFetch)
        "/jobs",
        # ADR-0142's two downloads: the owner has not decided (2026-10-31)
        "/vault/export",
    ),
}
