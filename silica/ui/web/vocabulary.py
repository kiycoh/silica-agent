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

from silica.tools import TOOLS, note_keys


def table() -> dict[str, Any]:
    """Asked per page: what a process withholds is its unit's, and read per call."""
    from silica.tools import catalog

    catalog.load()  # a page served before the app's startup ran still names every tool
    return {
        "tools": {
            "labels": {name: tool.label for name, tool in TOOLS.items() if tool.label},
            "writes": {name: tool.mutates for name, tool in TOOLS.items() if tool.mutates},
            "note_args": note_keys(),
        },
    }


def island() -> str:
    """`table()` as a script element's text. `<` is escaped because the parser
    ends the element at the first `</script>`, whatever the JSON means by it."""
    return json.dumps(table(), ensure_ascii=False).replace("<", "\\u003c")
