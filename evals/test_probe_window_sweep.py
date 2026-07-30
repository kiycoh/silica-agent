# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""probe_window_sweep on a synthetic fixture. Offline: cooccur only.

The fixture is adversarial by construction: the gold answer sits FAR from the
query-term-dense region of a long body, so a narrow density-anchored window
loses it while a wide one keeps it. That is exactly the failure mode the sweep
exists to measure — a fixture where the answer sits inside the dense region
would score every width 1.0 and verify nothing.
"""
from __future__ import annotations

import json


# ~2400 chars dense in query terms, answer at the far end: a 3000-char window
# takes the whole body, a 750-char window anchors on density and misses it.
_DENSE = "the yoga class schedule question came up again today. " * 44
_GOLD_BODY = _DENSE + "Final decision: the class meets on Thursday at dawn."


def _inst(qid: str, n_sessions: int, answer: str = "Thursday at dawn") -> dict:
    sids = [f"s{i}" for i in range(n_sessions)]
    sessions = []
    for i, sid in enumerate(sids):
        if sid == "s1":
            turns = [{"role": "user", "content": _GOLD_BODY}]
        else:
            turns = [{"role": "user",
                      "content": f"is my yoga class still on this week ({i})"}]
        sessions.append(turns)
    return {"question_id": qid, "question_type": "single-session-user",
            "question": "when does my yoga class meet?", "answer": answer,
            "question_date": "2026-05-01",
            "haystack_session_ids": sids,
            "haystack_dates": ["2026-01-01"] * n_sessions,
            "haystack_sessions": sessions,
            "answer_session_ids": ["s1"]}


def test_width_for_applies_rank_bands():
    from evals.probe_window_sweep import width_for

    bands = [[3, 3000], [8, 1500], [None, 750]]
    assert [width_for(r, bands) for r in (1, 3, 4, 8, 9, 15)] == \
        [3000, 3000, 1500, 1500, 750, 750]
    assert width_for(5, [[None, 2250]]) == 2250  # uniform = one open band


def test_narrow_window_loses_the_far_answer_wide_keeps_it(tmp_path):
    from evals.probe_window_sweep import main

    data_path = tmp_path / "lme.json"
    data_path.write_text(json.dumps([_inst("q1", 5)]), encoding="utf-8")
    out = tmp_path / "rep.json"
    rc = main(["--data", str(data_path), "--run-root", str(tmp_path / "v"),
               "--k", "5", "--no-embed", "--no-rerank", "--out", str(out)])
    assert rc == 0

    doc = json.loads(out.read_text(encoding="utf-8"))
    cells = doc["questions"][0]["cells"]
    # Body is ~2450 chars: 1x3000 passes it whole, the answer survives.
    assert cells["1x3000"]["gic"] is True
    # 1x750 anchors on the dense region ~2 windows away from the answer.
    assert cells["1x750"]["gic"] is False
    # Cost is monotone in width.
    assert cells["1x3000"]["chars"] > cells["1x1500"]["chars"] > cells["1x750"]["chars"]

    # Block-level survival mirrors the context-level result, band-attributed.
    surv = {(s["cell"], s["band"]): s["survived"]
            for s in doc["questions"][0]["survival"]}
    gold_rank = next(b["rank"] for b in doc["questions"][0]["blocks"] if b["gold"])
    band = "1-3" if gold_rank <= 3 else ("4-8" if gold_rank <= 8 else "9+")
    assert surv[("1x3000", band)] is True
    assert surv[("1x750", band)] is False
    # Evidence is recorded per block for the offline adaptive-N analysis.
    assert all("evidence" in b for b in doc["questions"][0]["blocks"])


def test_derived_gold_is_excluded_not_scored_false(tmp_path):
    """A numeric gold ('3') has no content tokens: gic must be None and the
    aggregate must exclude the question, never count it as a window loss."""
    from evals.probe_window_sweep import main

    data_path = tmp_path / "lme.json"
    data_path.write_text(json.dumps([_inst("q1", 4, answer="3")]),
                         encoding="utf-8")
    out = tmp_path / "rep.json"
    main(["--data", str(data_path), "--run-root", str(tmp_path / "v"),
          "--k", "4", "--no-embed", "--no-rerank", "--out", str(out)])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["questions"][0]["cells"]["1x3000"]["gic"] is None
    assert doc["report"]["1x3000"]["gic"] is None  # excluded, not False
    assert any("derived golds" in n for n in doc["notes"])
