"""Near-title dedup WorkItems carry the real fuzzy title ratio as title_score,
so the run's ledger records how close the titles were and not a degenerate 0.0.
"""
from types import SimpleNamespace

from silica.router.states.distill import enqueue_near_title_dedups


class _FakeQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, item):
        self.items.append(item)


def _fsm(queue):
    return SimpleNamespace(
        work_queue=queue, inbox_file="in.md", current_source_file="in.md",
        hub="Hub", current_content_hash="h", target_dir="TargetDir",
        progress=SimpleNamespace(run_id="run-1"),
    )


def test_ratio_becomes_title_score():
    q = _FakeQueue()
    rejected = [{
        "op": {"heading": "Beta", "snippet": "beta body"},
        "reason": "near_title candidate='Betas' path='TargetDir/Betas.md' "
                  "ratio=0.90 — deferred for dedup review",
    }]
    enqueue_near_title_dedups(_fsm(q), rejected)
    assert len(q.items) == 1
    job = q.items[0].context
    assert job.title_score == 0.90              # real ratio, not 0.0


def test_missing_ratio_stays_zero():
    q = _FakeQueue()
    rejected = [{
        "op": {"heading": "Beta", "snippet": "x"},
        "reason": "near_title candidate='Betas' path='TargetDir/Betas.md'",
    }]
    enqueue_near_title_dedups(_fsm(q), rejected)
    assert q.items[0].context.title_score == 0.0  # graceful when absent


def test_rejections_against_one_candidate_become_one_judge_call():
    """N near-title rejections of the same note must enqueue ONE family batch
    (the candidate body dominates the judge prompt; per-item calls re-send it
    N times), while a rejection against a different note stays its own item."""
    q = _FakeQueue()
    rejected = [
        {"op": {"heading": "Beta", "snippet": "b1"},
         "reason": "near_title candidate='Betas' path='TargetDir/Betas.md' ratio=0.90"},
        {"op": {"heading": "Beta variant", "snippet": "b2"},
         "reason": "near_title candidate='Betas' path='TargetDir/Betas.md' ratio=0.88"},
        {"op": {"heading": "Gamma", "snippet": "g"},
         "reason": "near_title candidate='Gammas' path='TargetDir/Gammas.md' ratio=0.91"},
    ]
    enqueue_near_title_dedups(_fsm(q), rejected)
    by_path = {it.target_path: it for it in q.items}
    assert len(q.items) == 2
    family = by_path["TargetDir/Betas.md"]
    # Two concepts, each keeping its own fuzzy ratio.
    scores = sorted(c.title_score for c in family.context.members)
    assert scores == [0.88, 0.90]
    # The singleton passes through unbatched, shape unchanged.
    assert by_path["TargetDir/Gammas.md"].context.title_score == 0.91
