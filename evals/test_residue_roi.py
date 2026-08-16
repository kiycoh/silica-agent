"""residue_roi core: mapping and rates, judge mocked (no LLM)."""
import json

from evals.residue_roi import roi_report


def _run_dir(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Concepts").mkdir(parents=True)
    (vault / "Concepts" / "Alpha.md").write_text("body alpha", encoding="utf-8")
    (vault / "Concepts" / "Beta.md").write_text("body beta", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "ledger.json").write_text(json.dumps({
        "vault": str(vault),
        "inputs": {
            "inbox_files": ["Inbox/src.md"],
            "residue_rounds": {
                "f0": {
                    "check": {"facts": ["fact a", "fact b", "fact c"], "total": 3},
                    "declared": {"facts": ["fact c", "fact d"], "total": 2},
                },
            },
        },
    }), encoding="utf-8")
    (run / "manifest.json").write_text(json.dumps({
        "run_id": "x",
        "entries": [
            {"title": "Alpha", "path": "Concepts/Alpha",
             "source_basename": "src.md", "op": "write"},
            {"title": "Beta", "path": "Concepts/Beta",
             "source_basename": "src.md", "op": "patch"},
            {"title": "Other", "path": "Concepts/Other",
             "source_basename": "other.md", "op": "write"},
        ],
    }), encoding="utf-8")
    return str(run)


def test_roi_report_maps_notes_and_computes_rates(tmp_path):
    calls = []
    # steer facts: a recovered, b not, c judge-failure; declared: c present
    # in the notes (check false positive), d genuinely missing.
    verdicts = {"fact a": True, "fact b": False, "fact c": None}
    verdicts_declared = {"fact c": True, "fact d": False}

    def judge(facts, source):
        calls.append((list(facts), source))
        table = verdicts if "fact a" in facts else verdicts_declared
        return [table.get(f) for f in facts]

    rep = roi_report(_run_dir(tmp_path), judge=judge)
    f0 = rep["files"]["f0"]
    # notes text fed to the judge contains ONLY this source's notes
    for _, src in calls:
        assert "body alpha" in src and "body beta" in src

    assert f0["recovery"] == {"supported": 1, "judged": 2, "failures": 1}
    assert f0["false_positive"] == {"supported": 1, "judged": 2, "failures": 0}
    assert rep["totals"]["recovery_rate"] == 0.5
    assert rep["totals"]["fp_rate"] == 0.5


def test_roi_report_skips_recovery_without_a_round(tmp_path):
    run = _run_dir(tmp_path)
    led_path = f"{run}/ledger.json"
    led = json.loads(open(led_path).read())
    del led["inputs"]["residue_rounds"]["f0"]["declared"]  # check ran, no round
    open(led_path, "w").write(json.dumps(led))

    rep = roi_report(run, judge=lambda facts, source: [True for _ in facts])
    assert "recovery" not in rep["files"]["f0"]
    assert "false_positive" not in rep["files"]["f0"]
