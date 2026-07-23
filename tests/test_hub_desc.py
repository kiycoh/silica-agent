# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""_hub_desc: the MOC bullet text derived from a note body must be clean prose,
not the raw first line — which was often a fabricated callout, producing garbage
bullets like `- [[X]] — > [!NOTE] Documento originale: ...` (audit finding 3)."""
from silica.router.states.write import _hub_desc


def test_strips_callout_syntax_from_first_line():
    # The audit fix is "strip callout/markdown from the first line": the markdown
    # syntax must go, leaving clean text — no `> [!NOTE]` in the bullet.
    body = "> [!NOTE] Documento originale: lezione 3\n\nLa normalizzazione riscala le feature."
    assert _hub_desc(body) == "Documento originale: lezione 3"


def test_strips_heading_and_list_markers():
    assert _hub_desc("# Titolo") == "Titolo"
    assert _hub_desc("- primo punto") == "primo punto"


def test_skips_purely_structural_first_line_to_next():
    # A bare callout marker with no inline text falls through to the prose line.
    assert _hub_desc("> [!NOTE]\n\ntesto reale") == "testo reale"


def test_plain_first_line_passes_through():
    assert _hub_desc("Una definizione chiara del concetto.") == "Una definizione chiara del concetto."


def test_caps_length():
    assert len(_hub_desc("x " * 200)) <= 120


def test_empty_body_is_empty_desc():
    assert _hub_desc("") == ""
    assert _hub_desc("\n\n> [!NOTE]\n") == ""
