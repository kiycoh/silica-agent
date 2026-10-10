# SPDX-License-Identifier: AGPL-3.0-or-later
"""B6: `/nucleate Inbox/*` (the README's documented batch form) must expand the
glob deterministically and reach the FSM, never fall through to the agent."""

import silica.router.coordinator as rc


def _capture_coordinator(monkeypatch) -> dict:
    """Stub the Coordinator seam; returns the kwargs dict it was built with."""
    calls: dict = {}

    class _FakeCoordinator:
        def __init__(self, **kw):
            calls.update(kw)

        def run(self):
            return {"final_status": "done"}

    monkeypatch.setattr(rc, "Coordinator", _FakeCoordinator)
    return calls


def test_glob_expands_to_the_inbox_files_and_reaches_the_fsm(tmp_vault, monkeypatch):
    calls = _capture_coordinator(monkeypatch)
    tmp_vault.note("Inbox/a.md", "alpha body\n")
    tmp_vault.note("Inbox/b.md", "beta body\n")
    from silica.cli import _expand_workflow_shortcut

    out = _expand_workflow_shortcut("/nucleate Inbox/* --target=Concepts")

    assert out == ""  # handled inline, no agent turn
    assert sorted(calls.get("inbox_files", [])) == ["Inbox/a.md", "Inbox/b.md"]
    assert calls.get("target_dir") == "Concepts"


def test_glob_with_no_match_reports_and_stays_deterministic(tmp_vault, monkeypatch):
    calls = _capture_coordinator(monkeypatch)
    from silica.cli import _expand_workflow_shortcut

    out = _expand_workflow_shortcut("/nucleate Inbox/*.pdf --target=Concepts")

    assert out == ""  # a miss is an answer, not a question for the agent
    assert calls == {}  # nothing to nucleate, nothing dispatched


def test_glob_matching_a_directory_expands_its_notes(tmp_vault, monkeypatch):
    """`Inbox/*` may match a subfolder; it takes the existing folder branch."""
    calls = _capture_coordinator(monkeypatch)
    tmp_vault.note("Inbox/sub/c.md", "gamma body\n")
    from silica.cli import _expand_workflow_shortcut

    out = _expand_workflow_shortcut("/nucleate Inbox/* --target=Concepts")

    assert out == ""
    assert calls.get("inbox_files") == ["Inbox/sub/c.md"]
