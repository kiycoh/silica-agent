from silica.kernel.assembly import Unit, fill_budget


def test_seeds_never_trimmed_even_over_budget():
    seeds = [Unit(path="a", text="x" * 5000, is_seed=True, rank=0)]
    kept, trunc = fill_budget(seeds, [], budget=3000)
    assert [u.path for u in kept] == ["a"]        # protect-seeds invariant
    assert trunc.dropped == []


def test_periphery_fills_by_rank_then_reports_drops():
    seeds = [Unit(path="s", text="x" * 1000, is_seed=True, rank=0)]
    periphery = [
        Unit(path="p1", text="y" * 800, is_seed=False, rank=0),
        Unit(path="p2", text="z" * 800, is_seed=False, rank=1),
        Unit(path="p3", text="w" * 800, is_seed=False, rank=2),
    ]
    kept, trunc = fill_budget(seeds, periphery, budget=2600)  # room for s + p1 + p2
    assert [u.path for u in kept] == ["s", "p1", "p2"]
    assert trunc.dropped == ["p3"]
    assert trunc.kept == 3
