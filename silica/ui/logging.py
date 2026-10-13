# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

import io
import json
import os
import traceback
from datetime import datetime, timezone
from typing import Any, cast
import logging
import sys

from rich.console import Console

from silica.ui.console import CONSOLE
from silica.ui.style import GLYPHS
from silica.ui.console import SILICA_THEME


class LiveAwareStreamHandler(logging.StreamHandler):
    """StreamHandler that resolves ``sys.stderr`` at emit time instead of caching it.

    A ``rich.live.Live`` redirects ``sys.stderr`` to a proxy that prints above the
    live region; a handler holding the original stream writes raw and tears the
    render. Reading ``sys.stderr`` dynamically lets the log flow through that proxy
    while a Live is active, and through the real stream otherwise.
    """

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass  # always dynamic — ignore the value StreamHandler.__init__ assigns

    def emit(self, record: logging.LogRecord) -> None:
        # Do NOT wrap this in CONSOLE._lock: while a Live is active, writing to
        # the redirected stderr re-enters rich's own Live.process_renderables(),
        # which acquires Live._lock (a *different* RLock). A background thread
        # holding Console._lock while blocking on Live._lock, concurrently with
        # the main thread holding Live._lock (inside Live.update()) and wanting
        # Console._lock, is a classic two-lock deadlock — reproduced live via
        # py-spy: two identical stack dumps seconds apart, zero progress.
        # rich's own Live._lock/Console._lock already serialize concurrent
        # writes; adding a second, independently-ordered lock on top is what
        # turns that safe serialization into a cycle. Accept the pre-existing
        # rare torn-panel-line race instead of a guaranteed hang.
        super().emit(record)


class HumanFriendlyFormatter(logging.Formatter):
    """Log formatter: timestamp + level icon in Rich markup, long messages truncated."""

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, self.datefmt)

        level = record.levelno
        if level == logging.DEBUG:
            icon = f"[muted]{GLYPHS['gear']}[/muted]"
        elif level == logging.INFO:
            icon = f"[tool.ok]{GLYPHS['info']}[/tool.ok]"
        elif level == logging.WARNING:
            icon = f"[warn]{GLYPHS['warn']}[/warn]"
        elif level >= logging.ERROR:
            icon = f"[tool.err]{GLYPHS['err']}[/tool.err]"
        else:
            icon = GLYPHS["bullet"]

        try:
            message = record.getMessage()
        except Exception as e:
            message = f"{record.msg} (args: {record.args}) [formatting error: {e}]"

        lines = message.split("\n")
        if len(lines) > 15:
            head = lines[:5]
            tail = lines[-5:]
            hidden = len(lines) - 10
            message = "\n".join(head + [f"  [dim]... ({hidden} lines truncated) ...[/dim]"] + tail)

        return f"  [muted][{time_str}][/muted] {icon} {message}"


class AnsiHumanFriendlyFormatter(HumanFriendlyFormatter):
    """HumanFriendlyFormatter for worker threads: renders the markup to ANSI here.

    The main-thread RichHandler renders markup through the shared CONSOLE; a
    worker thread can't — concurrent rich rendering onto an active Live tears the
    terminal (see ``_setup_logging``). So we render this one record to its own
    throwaway Console/buffer (nothing shared → thread-safe) and hand plain ANSI
    text to the StreamHandler, which the live-aware stderr proxy prints above the
    Live region intact. The buffer Console mirrors CONSOLE's terminal/colour, so
    piped output stays plain — exactly like the main-thread path.
    """

    def format(self, record: logging.LogRecord) -> str:
        markup = super().format(record)
        buf = io.StringIO()
        Console(
            file=buf,
            theme=SILICA_THEME,
            # highlight stays on: RichHandler applies its ReprHighlighter to
            # main-thread records, so worker records get the same treatment here.
            force_terminal=CONSOLE.is_terminal,
            # Rich's getter widens to str|None while the ctor wants its own
            # Literal; the value comes straight off a live Console, so it is
            # one of them by construction.
            color_system=cast(Any, CONSOLE.color_system),
            width=CONSOLE.width,
        ).print(markup, end="", soft_wrap=True)
        return buf.getvalue()


def _module_of(filename: str) -> str:
    """Dotted module for a traceback frame's file, best effort: the longest
    sys.path entry that holds it, else the bare file stem."""
    for root in sorted((p for p in sys.path if p), key=len, reverse=True):
        prefix = root.rstrip(os.sep) + os.sep
        if filename.startswith(prefix):
            return filename[len(prefix):].removesuffix(".py").replace(os.sep, ".")
    return os.path.splitext(os.path.basename(filename))[0]


class HostedJsonFormatter(logging.Formatter):
    """One JSON line per record for a hosted tenant's journal (ADR-0085 point 8,
    built by ADR-0100 point 2).

    A record keeps its template and never its arguments: `msg` is the format
    string before `%`, an exception is its type and its frames, and str(exc),
    args and locals are never written, because any of them can carry a
    student's notes or a provider's key. A logger outside `silica.*` loses
    `msg` as well: litellm builds its messages with f-strings, the exception's
    text included, so there is no template to keep. `silica.metrics` records
    (silica/metrics.py) are written as their event and fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        if record.name == "silica.metrics":
            return json.dumps({"ts": ts, "event": str(record.msg), **getattr(record, "metrics", {})},
                              ensure_ascii=False, separators=(",", ":"))
        out: dict[str, Any] = {"ts": ts, "logger": record.name, "level": record.levelname}
        if record.name == "silica" or record.name.startswith("silica."):
            # logger.warning(exc) passes the exception itself as msg: its
            # str() is the text the template rule keeps out, so only its type.
            out["msg"] = record.msg if isinstance(record.msg, str) else f"<{type(record.msg).__name__}>"
        if record.exc_info and record.exc_info[0] is not None:
            cls, tb = record.exc_info[0], record.exc_info[2]
            out["exc"] = {
                "type": cls.__qualname__ if cls.__module__ == "builtins"
                else f"{cls.__module__}.{cls.__qualname__}",
                "frames": [f"{_module_of(f.filename)}:{f.name}:{f.lineno}"
                           for f in traceback.extract_tb(tb)],
            }
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
