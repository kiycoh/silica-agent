"""Ctrl+C during a nucleate run must not commit new notes.

_run_loop's finally used to call _boundary_anneal() unconditionally, so an
interrupt triggered a deferred-store sweep that wrote notes ~30s after the user
cancelled — and never journalled them for /revert (CLEANUP never runs).
"""
from silica.router.base_fsm import BaseFSM
from silica.router.orchestrator import InjectorFSM


def _fsm(monkeypatch, calls, exc):
    fsm = InjectorFSM("Inbox/test.md", "TargetDir")
    monkeypatch.setattr(fsm, "_boundary_anneal", lambda: calls.append("anneal"))
    monkeypatch.setattr(fsm, "_flush_indexes", lambda: calls.append("flush"))

    def boom(self):
        raise exc

    monkeypatch.setattr(BaseFSM, "_run_loop", boom)  # what super()._run_loop() resolves to
    return fsm


def test_interrupt_skips_anneal_but_still_flushes(monkeypatch):
    calls: list[str] = []
    fsm = _fsm(monkeypatch, calls, KeyboardInterrupt())
    try:
        fsm._run_loop()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt must propagate")
    assert calls == ["flush"], calls  # index flush is cleanup; anneal is new writes


def test_crash_still_anneals(monkeypatch):
    """A crash is not a cancel — deferred bundles stay recoverable."""
    calls: list[str] = []
    fsm = _fsm(monkeypatch, calls, RuntimeError("boom"))
    try:
        fsm._run_loop()
    except RuntimeError:
        pass
    assert calls == ["anneal", "flush"], calls


def test_interrupt_leaves_a_marker_outside_the_vault(monkeypatch, tmp_path):
    """The end-of-run link sweep never runs after Ctrl+C: 34 forward refs of an
    interrupted 14-lecture run stayed dangling, and the next run swept only its
    own notes (2026-10-12). The marker lives in the run's ledger dir, so the
    interrupt still writes nothing to the vault; the next run reads it."""
    import silica.kernel.progress as prog
    monkeypatch.setattr(prog, "_RUNS_DIR", tmp_path / "runs")
    calls: list[str] = []
    fsm = _fsm(monkeypatch, calls, KeyboardInterrupt())
    try:
        fsm._run_loop()
    except KeyboardInterrupt:
        pass
    assert (fsm.progress.run_dir / "interrupted").exists()


def test_a_crash_leaves_no_interrupt_marker(monkeypatch, tmp_path):
    import silica.kernel.progress as prog
    monkeypatch.setattr(prog, "_RUNS_DIR", tmp_path / "runs")
    fsm = _fsm(monkeypatch, [], RuntimeError("boom"))
    try:
        fsm._run_loop()
    except RuntimeError:
        pass
    assert not (fsm.progress.run_dir / "interrupted").exists()
