# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The words this server and its page share, written once and read by both.

The page used to hold its own copy of every table it needed, each kept in step by
a comment, and only two by a test. `index()` stamps this table into the page as a
JSON island instead: no route and no fetch, so it is there before the first script
runs, and a script reads `VOCAB` where it held a copy.

Nothing is authored here. Each entry is read off the table the Python side already
obeys, so a word changed there reaches the page in the same edit.
"""
from __future__ import annotations

import json
from typing import Any

from silica.kernel import user_memory
from silica.kernel.base.formats import IMAGE_MIME, SHOWN_IMAGE_EXTS, WEB_IMAGE_EXTS
from silica.kernel.check import ORDER
from silica.kernel.learn import grade, items, mastery, objectives, pace, pick, quiz, reasons, reference, session, sources
from silica.kernel.report import note_context
from silica.sources.discover import story
from silica.tools import TOOLS, note_keys
from silica.ui.web import discover_routes, failures, jobs, library_routes, rail, stt_routes
from silica.view_table import VIEWS


def table() -> dict[str, Any]:
    """Asked per page: what a process withholds is its unit's, and read per call."""
    from silica.config import withheld
    from silica.tools import catalog

    catalog.load()  # a page served before the app's startup ran still names every tool
    return {
        "tools": {
            "labels": {name: tool.label for name, tool in TOOLS.items() if tool.label},
            "writes": {name: tool.mutates for name, tool in TOOLS.items() if tool.mutates},
            "note_args": note_keys(),
        },
        "views": {"slugs": {tab: view.slug for tab, view in VIEWS.items()},
                  "withheld_commands": sorted(withheld("commands"))},
        "jobs": {"interrupted": jobs.INTERRUPTED, "unconverted": jobs.UNCONVERTED},
        "learn": {
            "confidence": quiz.CONFIDENCE,
            "ratings": {n: name.lower() for n, name in reasons.RATING_NAME.items()},
            "levels": mastery.DEPTH_LABEL,
            "objectives": objectives.LABEL,
            "objective_help": objectives.DESCRIPTION,
            "marks": reasons.MARK_SAY,
            "roles": sources.ROLE_LABEL,
            "parts": reference.PART_LABEL,
            "separator": reference.RESTRICT_SEP,
            "session": pick.DEFAULT_N,
            "session_max": pace.SESSION_MAX,
            "minutes_per_question": items.MINUTES_PER_QUESTION,
            "teach_turns": grade.TEACH_TURNS,
            "session_gone": session.Gone.code,
        },
        "memory": {"verbs": user_memory.VERBS},
        "check": {"grades": ORDER},
        "limits": {"phrases": failures.LIMIT_PHRASE, "sentences": failures.LIMIT_SENTENCE,
                   "resets": failures.LIMIT_RESETS},
        "discover": {"tabs": discover_routes.TABS, "desktop_only": story.DESKTOP_ONLY},
        "notes": {"unwritten": note_context.UNWRITTEN},
        "files": {"says": rail.FILE_SAYS},
        "mic": {"max_ms": stt_routes.MIC_MAX_MS, "explain_max_ms": stt_routes.MIC_EXPLAIN_MAX_MS},
        # The two differ on purpose: what the chat model is sent, and what a note
        # may hold (formats.WEB_IMAGE_EXTS, the hosted images). Then two lists of
        # extensions: what an embed draws as a picture, and what /file opens as one.
        "formats": {"chat_images": list(dict.fromkeys(IMAGE_MIME.values())),
                    "note_images": list(dict.fromkeys(IMAGE_MIME[e] for e in WEB_IMAGE_EXTS)),
                    "shown_images": list(SHOWN_IMAGE_EXTS),
                    "file_pictures": list(library_routes.FILE_PICTURES)},
    }


def island() -> str:
    """`table()` as a script element's text. `<` is escaped because the parser
    ends the element at the first `</script>`, whatever the JSON means by it."""
    return json.dumps(table(), ensure_ascii=False).replace("<", "\\u003c")
