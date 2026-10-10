# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""evals/learn_questions.py counts what a reader would see wrong (ADR-0065)."""
from __future__ import annotations

import json

from evals import learn_questions as lq


def test_stats_counts_kept_items_and_each_defect(tmp_path):
    for name, run, made in (
        ("a", {"asks": 3, "kept": 2}, [
            {"q": "Come funzionano gli agenti reattivi e cosa non fanno?", "criteria": "regole se-allora; niente memoria"},
            {"q": "Qual è la definizione di intelligenza fornita nella nota?", "criteria": "The answer must name it"}]),
        ("b", {"asks": 1, "kept": 1}, [
            {"q": "Perché la RuBisCO lega anche l'ossigeno?", "criteria": "a temperature alte"}]),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "run.json").write_text(json.dumps(run))
        (tmp_path / name / "made.json").write_text(json.dumps(made))
    st = lq.stats(tmp_path)
    assert (st["notes"], st["asks"], st["kept"]) == (2, 4, 3)
    assert st["compound"] == ["Come funzionano gli agenti reattivi e cosa non fanno?"]
    assert st["text_ref"] == ["Qual è la definizione di intelligenza fornita nella nota?"]
    assert st["english_criteria"] == ["Qual è la definizione di intelligenza fornita nella nota?"]
    assert st["over_25_words"] == []
    assert not lq.COMPOUND.search("Qual è la differenza tra fase luminosa e ciclo di Calvin?")
