#!/usr/bin/env python3
"""PreToolUse guard: keep Bash commands on `uv`, the project's only env manager.

Blocks the two things that actually desync the uv-managed .venv / uv.lock:
  - `pip install` run outside uv (use `uv add` / `uv pip install`)
  - a bare `pytest` invocation (use `uv run pytest`)

Wired from .claude/settings.json. Exit 2 = block; stderr is shown to the model.
Run `python3 .claude/hooks/uv-guard.py --selftest` to check the matcher.
"""
import json
import re
import sys

# Match only at the start of a command *segment* (^, or after ; & | newline), so a
# literal "pip install" / "pytest" inside a quoted arg or commit message is ignored.
# ponytail: segment-anchored regex, not a shell lexer; a pipeline like
# `echo x | pytest` would slip through. Swap in shlex if that ever bites.
_SEG = r"(?:^|[;&|\n])\s*"
_PIP = re.compile(_SEG + r"(?:sudo\s+)?(?:python[0-9.]*\s+-m\s+)?pip[0-9.]*\s+install\b")
_PYTEST = re.compile(_SEG + r"pytest\b")


def check(cmd: str) -> str | None:
    """Return a block reason, or None to allow."""
    if _PIP.search(cmd):
        return "pip install desyncs the uv env. Use `uv add <pkg>` (or `uv pip install`)."
    if _PYTEST.search(cmd):
        return "Bare pytest may use the wrong interpreter. Use `uv run pytest`."
    return None


def _selftest() -> None:
    assert check("pip install foo")
    assert check("pip3 install foo")
    assert check("python -m pip install foo")
    assert check("sudo pip install foo")
    assert check("cd x && pip install foo")
    assert check("pytest tests/")
    assert check("cd x && pytest -k thing")
    assert check("uv add foo") is None
    assert check("uv pip install foo") is None
    assert check("uv run pip install foo") is None
    assert check("uv run pytest -q") is None
    assert check("git commit -m 'fix pip install docs'") is None
    assert check("git commit -m 'flaky pytest run'") is None
    print("uv-guard selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    reason = check(cmd)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)
