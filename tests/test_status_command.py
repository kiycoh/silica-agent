"""/status renders the ledger digest as preformatted text.

Regression: it used to go through FlatMarkdown, which reflowed every line of
the digest into a single wrapped paragraph.
"""

from silica.cli import _handle_direct_shortcut


def test_status_keeps_line_structure(tmp_path, monkeypatch, capsys):
    import silica.kernel.progress as prog_mod
    monkeypatch.setattr(prog_mod, "_RUNS_DIR", tmp_path / "runs")

    from silica.kernel.progress import PlanStep, ProgressLedger, TaskLedger

    progress = ProgressLedger.new(mode="inject", inputs={"target_dir": "TargetDir"})
    (tmp_path / "runs" / progress.run_id).mkdir(parents=True)
    recon = progress.add_task("recon")
    progress.add_task("payload")
    progress.save()
    TaskLedger.new(
        run_id=progress.run_id,
        user_request="inject Inbox/a.md -> TargetDir",
        checkpoints=[
            PlanStep(id="recon", kind="mechanical", objective="scan"),
            PlanStep(id="payload", kind="mechanical", objective="build"),
        ],
    ).save()

    assert _handle_direct_shortcut(f"/status {progress.run_id}", [])
    lines = capsys.readouterr().out.splitlines()

    # Each section owns its line instead of being glued into one paragraph,
    # and the bracketed counts survive (markup=False).
    assert any(line.startswith("PLAN  [2 checkpoints]") for line in lines)
    assert any(line.startswith("PROGRESS  [pending=2]") for line in lines)
    assert any(line.strip() == f"· {recon.id}" for line in lines)
