from evals.probe_decision_sites import _auc, _mcnemar, _spearman


def test_stats():
    assert _auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert _auc([0.5], [0.5]) == 0.5
    assert _auc([], [0.1]) is None
    assert _spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert _spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert _mcnemar([True] * 5, [True] * 5) == 1.0
    assert _mcnemar([True] * 10, [False] * 10) == 0.002
