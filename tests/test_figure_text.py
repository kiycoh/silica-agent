"""ADR-0077 point 1: a figure's words reach the index, never a distill payload."""
from __future__ import annotations

from silica.kernel.text.media import figures_as_text, strip_images

CAPTIONED = (
    "Il test di Turing.\n\n"
    "![](images/45ee4f4ee5a2d721.jpg)\n\n"
    "<details>\n<summary>flowchart</summary>\n\n"
    "```mermaid\ngraph LR\n  A[\"User\"] --> B[\"Computer\"]\n```\n"
    "</details>\n\n"
    "Una macchina che conversa."
)


def test_a_converter_caption_becomes_the_figures_words():
    out = figures_as_text(CAPTIONED)
    assert "[figure: flowchart:" in out
    assert "User" in out and "Computer" in out
    assert "<details>" not in out and "45ee4f4ee5a2d721" not in out
    assert out.startswith("Il test di Turing.") and out.endswith("Una macchina che conversa.")


def test_an_uncaptioned_embed_reads_exactly_as_stripped():
    # Byte-identical, or every note embedding a screenshot would re-embed for nothing.
    text = "Prima\n![[Pasted image 20250716182448.png]]\nDopo ![](x/y.png) fine"
    assert figures_as_text(text) == strip_images(text)


def test_a_long_caption_cannot_push_the_prose_out_of_the_window():
    table = "| a | b |\n" * 600
    text = f"![](images/t.jpg)\n\n<details>\n<summary>table</summary>\n\n{table}</details>\n\nprosa"
    figure = figures_as_text(text).split("\n\nprosa")[0]
    assert len(figure) <= 320


def test_the_embedding_text_carries_the_caption():
    from silica.kernel.recall.embed import _note_text
    assert "Computer" in _note_text("Turing", CAPTIONED)


def test_the_lexical_index_holds_caption_words_and_not_file_names():
    from pathlib import Path
    from silica.kernel.recall.lexical import LexicalStore
    store = LexicalStore(Path("/dev/null"))
    store.upsert("Inbox/Lezione 1", "Lezione 1", CAPTIONED.replace("![](images/45ee4f4ee5a2d721.jpg)",
                                                                    "![[45ee4f4ee5a2d721.jpg]]"))
    terms = store._docs["Inbox/Lezione 1"]
    assert "computer" in terms
    assert not any("45ee4f4ee5a2d721" in t for t in terms)


def test_the_outline_lane_never_reads_a_caption(monkeypatch):
    import pytest
    from silica.kernel import outline as ol
    seen = {}

    def stop(src, **_):
        seen["src"] = src
        raise RuntimeError("captured")

    monkeypatch.setattr(ol, "skeleton", stop)
    with pytest.raises(RuntimeError):
        ol.run_outliner(source_text=CAPTIONED, source_basename="Lezione 1", target="", hub=None,
                        language="it", vault_outline=[], ask=lambda *a, **k: {})
    assert "Computer" not in seen["src"] and "<details>" not in seen["src"]
    assert "Una macchina che conversa." in seen["src"]


def test_learn_never_decomposes_a_caption():
    from silica.kernel.learn import inventory
    seen = {}

    def ask(system, user, **_):
        seen["user"] = user
        return {"claims": []}

    sec = {"section": "Turing", "text": "## Turing\n\n" + CAPTIONED}
    inventory._decompose(sec, ideas=[], language="it", ask=ask, stem="Lezione 1",
                         leaf_raw=CAPTIONED, body=CAPTIONED)
    assert "Computer" not in seen["user"] and "Una macchina che conversa." in seen["user"]
