# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's figures land beside the paragraph they illustrate (ADR-0082)."""
import json

from silica.agent.figures import place_figures

NOTE = (
    "# Hash table\n\n"
    "Una hash table mappa le chiavi in indici di un array tramite una funzione hash.\n\n"
    "![[La tabella hash_image_1.png]]\n"
    "> [figure, read by qwen3.5-9b] diagram: chiavi, indici e coppie chiave-valore\n\n"
    "## Collisioni\n\nDue chiavi diverse possono finire nello stesso indice.\n"
)


def _turn(answer: str, result: str = NOTE) -> list[dict]:
    return [{"role": "user", "content": "spiegami le hash table"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": result},
            {"role": "assistant", "content": answer}]


def test_figure_goes_after_the_paragraph_that_retells_its_neighbourhood():
    answer = ("Ecco come funziona.\n\n"
              "La hash table usa una funzione hash per mappare ogni chiave in un indice dell'array.\n\n"
              "Le collisioni si gestiscono a parte.")
    msgs = _turn(answer)
    out = place_figures(answer, msgs)
    blocks = out.split("\n\n")
    assert blocks[2] == "![[La tabella hash_image_1.png]]", out
    assert msgs[-1]["content"] == out  # a reopened chat shows what the live one showed


def test_nothing_placed_when_no_paragraph_shares_enough_words():
    answer = "Le collisioni si gestiscono con liste di trabocco."
    assert place_figures(answer, _turn(answer)) == answer


def test_a_figure_the_model_already_placed_is_not_doubled():
    answer = ("La hash table mappa ogni chiave in un indice dell'array con la funzione hash.\n\n"
              "![[La tabella hash_image_1.png]]")
    assert place_figures(answer, _turn(answer)) == answer


def test_recall_json_and_code_blocks():
    """A JSON result is read through its strings, and code is never the
    paragraph a figure goes after."""
    result = json.dumps({"query": "hash", "context": NOTE})
    answer = ("```python\n# hash table: mappa chiavi in indici con funzione hash\n```\n\n"
              "Una funzione hash mappa le chiavi in indici della tabella.")
    out = place_figures(answer, _turn(answer, result))
    assert out.endswith("della tabella.\n\n![[La tabella hash_image_1.png]]"), out


def test_figures_from_an_earlier_turn_stay_out():
    msgs = [{"role": "tool", "content": NOTE}] + _turn("x", result="nessuna figura qui")
    answer = "La hash table mappa le chiavi in indici con una funzione hash."
    msgs[-1]["content"] = answer
    assert place_figures(answer, msgs) == answer


def test_a_figure_whose_file_is_gone_is_left_out():
    answer = "La hash table usa una funzione hash per mappare ogni chiave in un indice dell'array."
    assert place_figures(answer, _turn(answer), exists=lambda f: False) == answer
    assert "![[" in place_figures(answer, _turn(answer), exists=lambda f: True)


def test_an_asset_name_is_found_literally_not_as_a_glob(tmp_path, monkeypatch):
    """_find_asset's basename fallback (shared by /asset and the placement) used
    the name as an rglob pattern: `fig[1].png` found fig1.png, and a `**` in a
    name raised ValueError while an answer was being finished."""
    from silica.config import CONFIG
    from silica.ui.web.server import _find_asset

    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "fig1.png").write_bytes(b"x")
    (tmp_path / "img" / "fig[1].png").write_bytes(b"x")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    assert _find_asset("fig[1].png") == (tmp_path / "img" / "fig[1].png").resolve()
    assert _find_asset("a**b.png") is None
