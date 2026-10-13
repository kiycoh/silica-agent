import logging
import re
import pytest

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
from silica.ui.logging import (
    AnsiHumanFriendlyFormatter,
    HumanFriendlyFormatter,
    LiveAwareStreamHandler,
)

def test_human_friendly_formatter_debug():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="silica.tools",
        level=logging.DEBUG,
        pathname="some_file.py",
        lineno=10,
        msg="Registered tool: %s (class=%s)",
        args=("test_tool", "TestClass"),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "⚙" in formatted
    assert "Registered tool: test_tool (class=TestClass)" in formatted

def test_human_friendly_formatter_info():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="silica.router.orchestrator",
        level=logging.INFO,
        pathname="orchestrator.py",
        lineno=50,
        msg="Restored %s to version %d",
        args=("note.md", 3),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "ℹ" in formatted
    assert "Restored note.md to version 3" in formatted

def test_human_friendly_formatter_warning():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="silica.driver.fs_backend",
        level=logging.WARNING,
        pathname="fs.py",
        lineno=100,
        msg="Failed to index %s: %s",
        args=("my_note.md", "Permission Denied"),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "⚠" in formatted
    assert "⚠️" not in formatted  # single-width glyph, no emoji variation selector
    assert "Failed to index my_note.md: Permission Denied" in formatted

def test_human_friendly_formatter_error():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="silica.router.orchestrator",
        level=logging.ERROR,
        pathname="fsm.py",
        lineno=200,
        msg="Rollback failed: %s",
        args=("Connection timed out",),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "✗" in formatted
    assert "Rollback failed: Connection timed out" in formatted

def test_human_friendly_formatter_unmapped_fallback():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="silica.some_new_module",
        level=logging.DEBUG,
        pathname="new.py",
        lineno=5,
        msg="Some unmapped technical detail %s",
        args=("value123",),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "⚙" in formatted
    assert "Some unmapped technical detail value123" in formatted

def test_human_friendly_formatter_non_silica_fallback():
    formatter = HumanFriendlyFormatter()
    record = logging.LogRecord(
        name="urllib3.connectionpool",
        level=logging.DEBUG,
        pathname="pool.py",
        lineno=400,
        msg="Starting new HTTPS connection (1): api.openai.com",
        args=(),
        exc_info=None
    )
    formatted = formatter.format(record)
    assert "⚙" in formatted
    assert "Starting new HTTPS connection (1): api.openai.com" in formatted

def test_human_friendly_formatter_bad_args_graceful_fallback():
    formatter = HumanFriendlyFormatter()
    # Template expects 2 args but we only pass 1 (which causes format to fail)
    record = logging.LogRecord(
        name="silica.tools",
        level=logging.DEBUG,
        pathname="some_file.py",
        lineno=10,
        msg="Registered tool: %s (class=%s)",
        args=("test_tool",),
        exc_info=None
    )
    formatted = formatter.format(record)
    # It should fallback gracefully to the standard %-formatted message or original message
    assert "⚙" in formatted
    assert "test_tool" in formatted


def _dedup_record():
    # A real worker-thread call site: it must read human-friendly, not raw.
    return logging.LogRecord(
        name="silica.capabilities.dedup",
        level=logging.INFO,
        pathname="dedup.py",
        lineno=1,
        msg="Restored %s to version %d",
        args=("note.md", 2),
        exc_info=None,
    )


def test_ansi_formatter_consumes_markup_and_rewords():
    """The worker seam must interpret markup, never emit literal [muted]/[/] tags.

    (The bug being guarded: writing HumanFriendlyFormatter's markup string raw
    to stderr, which prints the literal rich tags.)
    """
    out = AnsiHumanFriendlyFormatter().format(_dedup_record())
    # Strip ANSI: repr-highlighting wraps the "2" in colour codes on a terminal.
    assert "Restored note.md to version 2" in _ANSI.sub("", out)
    assert "[muted]" not in out and "[/muted]" not in out and "[/]" not in out


def test_ansi_formatter_emits_ansi_on_a_terminal(monkeypatch):
    """When CONSOLE is a colour terminal, the worker output carries ANSI codes
    (main-thread parity); when piped it stays plain."""
    class _FakeConsole:
        is_terminal = True
        color_system = "truecolor"
        width = 120

    monkeypatch.setattr("silica.ui.logging.CONSOLE", _FakeConsole())
    out = AnsiHumanFriendlyFormatter().format(_dedup_record())
    assert "\x1b[" in out  # ANSI escape present

    class _PipedConsole:
        is_terminal = False
        color_system = None
        width = 80

    monkeypatch.setattr("silica.ui.logging.CONSOLE", _PipedConsole())
    plain = AnsiHumanFriendlyFormatter().format(_dedup_record())
    assert "\x1b[" not in plain
    assert "Restored note.md to version 2" in plain


def test_ansi_formatter_applies_repr_highlighting(monkeypatch):
    """Worker records get ReprHighlighter colouring like RichHandler gives the
    main thread — numbers/strings/attribs coloured, not just the muted chrome.

    (The bug being guarded: highlight=False on the throwaway Console left worker
    lines visually plain next to highlighted main-thread ones.)
    """
    class _FakeConsole:
        is_terminal = True
        color_system = "truecolor"
        width = 200

    # Rich reads NO_COLOR when the throwaway Console is built, and this test
    # asserts colour: a developer shell that sets it must not fail the test.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("silica.ui.logging.CONSOLE", _FakeConsole())
    record = logging.LogRecord(
        name="silica.tools.cli_backend",
        level=logging.DEBUG,
        pathname="cli_backend.py",
        lineno=1,
        msg="CLI exec: %s  (timeout=%.1fs)",
        args=("obsidian read 'path=note.md'", 3.0),
        exc_info=None,
    )
    out = AnsiHumanFriendlyFormatter().format(record)
    # ReprHighlighter colours the quoted string green (32) and numbers cyan (36);
    # without it only the dim timestamp/icon spans exist.
    assert "\x1b[32m" in out and "\x1b[1;36m" in out


def test_bg_handler_does_not_take_console_lock():
    """A background emit must NOT wait on CONSOLE's lock.

    Emitting under an active Live re-enters rich's Live._lock; a thread holding
    Console._lock while wanting Live._lock, against a main thread holding
    Live._lock and wanting Console._lock, is a two-lock cycle that hangs the
    whole REPL. rich already serializes the writes, so this handler must add no
    lock of its own — the regression this pins is the hang, not a torn line.
    """
    import threading
    from silica.ui.console import CONSOLE

    handler = LiveAwareStreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    emitted = threading.Event()

    def _bg():
        handler.emit(logging.LogRecord("t", logging.INFO, "", 0, "hi", (), None))
        emitted.set()

    with CONSOLE._lock:  # stand in for a main thread mid-render
        t = threading.Thread(target=_bg)
        t.start()
        assert emitted.wait(1.0), "emit blocked on CONSOLE._lock — deadlock risk is back"
    t.join(timeout=1)


def test_no_root_handler_caches_real_stderr(monkeypatch):
    """Every stderr handler on root must resolve sys.stderr at emit time.

    A handler caching the real stream (plain StreamHandler(sys.stderr)) writes raw
    under an active rich.Live and tears the render — stale frames pile up in
    scrollback as duplicated text.
    """
    import io
    import sys
    from silica.cli import _setup_logging
    from silica.config import CONFIG
    from silica.ui.logging import LiveAwareStreamHandler

    orig_debug = CONFIG.debug_logging
    try:
        for debug in (True, False):
            _setup_logging(debug=debug)
            root = logging.getLogger()
            # No plain StreamHandler holding a cached stream on root
            assert not [h for h in root.handlers if type(h) is logging.StreamHandler]
            # Live-aware handlers follow a sys.stderr swap (rich.Live's redirect)
            proxy = io.StringIO()
            monkeypatch.setattr(sys, "stderr", proxy)
            live_aware = [h for h in root.handlers if isinstance(h, LiveAwareStreamHandler)]
            assert live_aware
            assert all(h.stream is proxy for h in live_aware)
            monkeypatch.undo()
    finally:
        _setup_logging(debug=orig_debug)


def test_gui_leaves_uvicorn_logging_to_root():
    """`serve()` must build its Server with log_config=None.

    uvicorn's own colourized formatter re-renders each record from the
    `color_message` extra, which still holds the raw `%d`/`%s`, and litellm
    >=1.98 attaches a redaction filter to `uvicorn.error` that interpolates the
    message and then clears `record.args`. On a TTY the two together printed the
    format string verbatim — "Started server process [%d]". Reproduced below on
    the real formatter so the assertion is about the interaction, not about our
    call site alone.
    """
    import inspect
    from silica.ui.web.server import serve

    src = inspect.getsource(serve)
    assert "log_config=None" in src

    uvicorn_logging = pytest.importorskip("uvicorn.logging")
    record = logging.LogRecord(
        name="uvicorn.error", level=logging.INFO, pathname="f", lineno=1,
        msg="Started server process [%d]", args=(1234,), exc_info=None,
    )
    record.color_message = "Started server process [%d]"
    record.msg, record.args = record.getMessage(), None  # what the filter does
    rendered = uvicorn_logging.DefaultFormatter(
        "%(levelprefix)s %(message)s", use_colors=True).format(record)
    assert "%d" in _ANSI.sub("", rendered)  # the formatter we are opting out of


# --- the file log (observability lever 7, 2026-09-06) --------------------------

def test_debug_records_land_in_the_file_log_while_the_terminal_stays_quiet(tmp_path, monkeypatch):
    """Every silica DEBUG line into ~/.silica/logs/silica.log; the terminal keeps
    WARNING. Third-party DEBUG (PIL, urllib3...) stays out of the file too, or
    the file is noise within a session."""
    import io
    import sys
    import silica.kernel.recall.paths as paths
    from silica.cli import _setup_logging
    from silica.config import CONFIG

    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path)
    orig = CONFIG.debug_logging
    try:
        _setup_logging(debug=False)
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        logging.getLogger("silica.probe").debug("needle-debug")
        logging.getLogger("silica.probe").warning("needle-warn")
        logging.getLogger("PIL.Image").debug("pil-noise")
        logging.getLogger("PIL.Image").warning("pil-warn")
        text = (tmp_path / "logs" / "silica.log").read_text(encoding="utf-8")
        assert "needle-debug" in text and "needle-warn" in text and "pil-warn" in text
        assert "pil-noise" not in text
        assert "needle-debug" not in err.getvalue()
        assert "needle-warn" in err.getvalue()
    finally:
        _setup_logging(debug=orig)


def test_attach_file_log_freezes_the_terminal_level_before_opening_root(tmp_path, monkeypatch):
    """The MCP wiring is basicConfig(WARNING): its handler has no level of its
    own, so lowering root to DEBUG for the file must first pin the terminal
    handler at what root used to show."""
    import io
    import silica.kernel.recall.paths as paths
    from silica.cli import _attach_file_log

    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        for h in root.handlers[:]:
            root.removeHandler(h)
        monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path)
        logging.basicConfig(stream=io.StringIO(), level=logging.WARNING)
        _attach_file_log()
        assert root.level == logging.DEBUG
        stream = [h for h in root.handlers if type(h) is logging.StreamHandler]
        assert stream and stream[0].level == logging.WARNING
        assert (tmp_path / "logs" / "silica.log").exists()
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)


def test_a_hosted_tenant_opens_no_file_log(tmp_path, monkeypatch):
    """ADR-0100 point 1: the notice promises the journal and 14 days, so under
    SILICA_HOSTED=1 no silica.log is opened and root keeps the terminal's level
    instead of dropping to DEBUG for a file that does not exist."""
    import io
    import silica.kernel.recall.paths as paths
    from silica.cli import _attach_file_log

    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        for h in root.handlers[:]:
            root.removeHandler(h)
        monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path)
        monkeypatch.setenv("SILICA_HOSTED", "1")
        logging.basicConfig(stream=io.StringIO(), level=logging.WARNING)
        _attach_file_log()
        assert root.level == logging.WARNING
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert not (tmp_path / "logs").exists()
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)
