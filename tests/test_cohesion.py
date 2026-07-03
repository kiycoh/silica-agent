"""Tests for the cohesion_pass sibling-linking step."""
from __future__ import annotations

from pathlib import Path

import pytest
from silica.kernel.cohesion import cohesion_pass, _content_tokens

_BUNDLED_OVERLAYS = (
    Path(__file__).resolve().parent.parent / "silica" / "overlays"
)


@pytest.fixture
def it_overlay():
    """Load the bundled Italian overlay."""
    path = _BUNDLED_OVERLAYS / "italian.yaml"
    if not path.exists():
        pytest.skip(f"bundled overlay not found: {path}")
    from silica.kernel.overlay import load_overlay
    return load_overlay(path)


# ---------------------------------------------------------------------------
# _content_tokens unit tests
# ---------------------------------------------------------------------------

def test_tokens_strips_stopwords():
    tokens = _content_tokens("II Framework PEAS Actuators")
    assert "ii" not in tokens
    assert "peas" in tokens
    assert "actuators" in tokens
    # "framework" is NOT a stopword — specific framework names (STRIPS, PEAS) need it
    assert "framework" in tokens


def test_tokens_empty_string():
    assert _content_tokens("") == frozenset()


def test_tokens_only_stopwords(it_overlay):
    """Italian function words are filtered by the italian overlay."""
    assert _content_tokens("di e in con su", overlay=it_overlay) == frozenset()


def test_tokens_min_length():
    # Single-character words are excluded (< 2 chars)
    tokens = _content_tokens("IA e DL")
    assert "e" not in tokens
    assert "ia" in tokens
    assert "dl" in tokens


def test_tokens_default_overlay_filters_english_structural():
    """DEFAULT overlay filters 'introduction' and 'the' but keeps a domain word."""
    tokens = _content_tokens("Introduction to Backpropagation")
    assert "introduction" not in tokens
    assert "the" not in tokens
    assert "backpropagation" in tokens


def test_tokens_it_overlay_filters_italian_function_and_structural(it_overlay):
    """italian overlay filters 'sistemi' and 'di' but keeps domain word 'reti'."""
    tokens = _content_tokens("Sistemi di Reti", overlay=it_overlay)
    assert "sistemi" not in tokens
    assert "di" not in tokens
    assert "reti" in tokens


def test_tokens_roman_numerals_filtered_regardless_of_overlay():
    """Roman numeral prefixes are filtered by _STRUCTURAL_TOKENS independent of overlay."""
    from silica.kernel.overlay import DEFAULT_OVERLAY
    tokens_default = _content_tokens("III Chapter Backpropagation", overlay=DEFAULT_OVERLAY)
    assert "iii" not in tokens_default
    assert "backpropagation" in tokens_default

    if (_BUNDLED_OVERLAYS / "italian.yaml").exists():
        from silica.kernel.overlay import load_overlay
        it_ov = load_overlay(_BUNDLED_OVERLAYS / "italian.yaml")
        tokens_it = _content_tokens("IV Reti Neurali", overlay=it_ov)
        assert "iv" not in tokens_it
        assert "reti" in tokens_it


# ---------------------------------------------------------------------------
# cohesion_pass — basic sibling detection
# ---------------------------------------------------------------------------

def _write_op(heading: str, title: str | None = None, related: list | None = None, source: str = "src.md") -> dict:
    op: dict = {
        "op": "write",
        "heading": heading,
        "path": f"Notes/{title or heading}.md",
        "source_basename": source,
        "hub": "Hub",
        "snippet": "...",
    }
    if title:
        op["title"] = title
    if related:
        op["related"] = related
    return op


def test_peas_siblings_all_linked():
    ops = [
        _write_op("II Framework PEAS Actuators", "PEAS Actuators"),
        _write_op("II Framework PEAS Sensors", "PEAS Sensors"),
        _write_op("II Framework PEAS Environment", "PEAS Environment"),
        _write_op("II Framework PEAS Performance Measure", "PEAS Performance Measure"),
    ]
    result = cohesion_pass(ops)
    # Every PEAS note should have the other three in its related list
    for i, op in enumerate(result):
        others = {result[j]["title"] for j in range(4) if j != i}
        assert set(op["related"]) >= others, f"op {i} missing siblings: {others - set(op['related'])}"


def test_two_sibling_ops_linked_bidirectionally():
    ops = [
        _write_op("Reti Neurali Convoluzionali"),
        _write_op("Reti Neurali Ricorrenti"),
    ]
    result = cohesion_pass(ops)
    assert "Reti Neurali Ricorrenti" in result[0]["related"]
    assert "Reti Neurali Convoluzionali" in result[1]["related"]


def test_no_siblings_when_no_shared_token():
    ops = [
        _write_op("Backpropagation"),
        _write_op("Alberi Decisionali"),
    ]
    result = cohesion_pass(ops)
    # No related injected — original dicts returned
    assert result[0].get("related") is None
    assert result[1].get("related") is None


def test_single_write_op_unchanged():
    ops = [_write_op("Backpropagation")]
    result = cohesion_pass(ops)
    assert result is ops  # fast path: same list returned


def test_zero_write_ops_unchanged():
    ops: list[dict] = []
    result = cohesion_pass(ops)
    assert result is ops


def test_non_write_ops_passed_through():
    ops = [
        {"op": "skip", "heading": "Foo", "source_basename": "s.md", "reason": "off-axis"},
        {"op": "patch", "heading": "Bar", "path": "Notes/Bar.md", "source_basename": "s.md", "snippet": "x"},
    ]
    result = cohesion_pass(ops)
    # No write ops → fast path
    assert result is ops


def test_existing_related_preserved_and_deduplicated():
    ops = [
        _write_op("PEAS Sensors", related=["Existing Note"]),
        _write_op("PEAS Actuators"),
    ]
    result = cohesion_pass(ops)
    related_sensors = result[0]["related"]
    assert "Existing Note" in related_sensors   # pre-existing entry preserved
    assert "PEAS Actuators" in related_sensors  # sibling injected
    assert related_sensors.count("PEAS Actuators") == 1  # no duplicate


def test_no_self_link():
    """An op must never appear in its own related list."""
    ops = [
        _write_op("PEAS Sensors"),
        _write_op("PEAS Actuators"),
    ]
    result = cohesion_pass(ops)
    assert "PEAS Sensors" not in result[0].get("related", [])
    assert "PEAS Actuators" not in result[1].get("related", [])


def test_title_preferred_over_heading_for_injection():
    """When title is set, siblings inject the title (not the raw heading)."""
    ops = [
        _write_op("II Framework PEAS Actuators", title="PEAS Actuators"),
        _write_op("II Framework PEAS Sensors", title="PEAS Sensors"),
    ]
    result = cohesion_pass(ops)
    # Injected name should be the title, not the compound heading
    assert "PEAS Sensors" in result[0]["related"]
    assert "II Framework PEAS Sensors" not in result[0]["related"]


def test_no_siblings_from_shared_italian_function_word_no_vault_overlay():
    """The bug this task fixes: on a vault with no overlay.yaml (active/default
    overlay is English), two Italian write ops whose display names share ONLY
    the Italian function word "cosa" must NOT be linked as siblings — per-op
    body-language detection must route each op through the Italian overlay,
    which filters "cosa" as a stopword, leaving no shared content token.

    Fails on pre-fix code because overlay=None resolved once via
    get_active_overlay() (English DEFAULT_OVERLAY, which does not filter
    "cosa"), so the two ops shared "cosa" as a false discriminating token.

    Verified directly: "cosa" in silica.kernel.language.stopwords_for("italian")
    is True, and "cosa" in stop_words.get_stop_words("en") is False — so the
    fixture only proves the intended thing if the body-language routing
    actually happens.
    """
    ops = [
        {
            "op": "write",
            "heading": "Cosa Cambia nei Sistemi Distribuiti",
            "title": "Cosa Cambia nei Sistemi Distribuiti",
            "path": "Notes/Cosa Cambia nei Sistemi Distribuiti.md",
            "source_basename": "src.md",
            "hub": "Hub",
            "snippet": (
                "Questo capitolo spiega cosa cambia quando un sistema informatico "
                "viene distribuito su piu macchine e come vengono gestiti i guasti "
                "della rete."
            ),
        },
        {
            "op": "write",
            "heading": "Cosa Serve per gli Algoritmi Genetici",
            "title": "Cosa Serve per gli Algoritmi Genetici",
            "path": "Notes/Cosa Serve per gli Algoritmi Genetici.md",
            "source_basename": "src.md",
            "hub": "Hub",
            "snippet": (
                "Questo capitolo spiega cosa serve per progettare un algoritmo "
                "genetico e come funziona la selezione naturale simulata al "
                "computer."
            ),
        },
    ]
    result = cohesion_pass(ops)
    assert result[0].get("related") is None
    assert result[1].get("related") is None


def test_overlay_file_wins_over_detected_body_language(tmp_vault):
    """A vault overlay.yaml is resolution order 1 (overlay_for_lang) — when present,
    overlay=None must use it regardless of what language the op body detects as.

    extends_default: false replaces the default stopword set entirely with just
    {"reti", "neurali"}. Neither DEFAULT_OVERLAY nor the bundled Italian overlay
    filters those two words, so the two ops below would be (wrongly) linked as
    siblings via "reti"/"neurali" under either — only honoring the vault file
    produces the correct "no siblings" outcome.
    """
    tmp_vault.note(
        "overlay.yaml",
        content=(
            "extends_default: false\n"
            "stopwords:\n"
            "  - reti\n"
            "  - neurali\n"
        ),
    )
    ops = [
        {
            "op": "write",
            "heading": "Reti Neurali Convoluzionali",
            "title": "Reti Neurali Convoluzionali",
            "path": "Notes/Reti Neurali Convoluzionali.md",
            "source_basename": "src.md",
            "hub": "Hub",
            "snippet": (
                "Questo capitolo descrive come funzionano le reti neurali "
                "convoluzionali applicate al riconoscimento delle immagini."
            ),
        },
        {
            "op": "write",
            "heading": "Reti Neurali Ricorrenti",
            "title": "Reti Neurali Ricorrenti",
            "path": "Notes/Reti Neurali Ricorrenti.md",
            "source_basename": "src.md",
            "hub": "Hub",
            "snippet": (
                "Questo capitolo descrive come funzionano le reti neurali "
                "ricorrenti applicate alla elaborazione del linguaggio."
            ),
        },
    ]
    result = cohesion_pass(ops)
    assert result[0].get("related") is None
    assert result[1].get("related") is None


def test_mixed_write_and_patch_only_writes_receive_siblings():
    ops = [
        _write_op("PEAS Sensors"),
        {"op": "patch", "heading": "PEAS Actuators", "path": "Notes/PEAS Actuators.md",
         "source_basename": "src.md", "snippet": "extra"},
        _write_op("PEAS Environment"),
    ]
    result = cohesion_pass(ops)
    # Only the two write ops are siblings
    assert "PEAS Environment" in result[0]["related"]
    assert "PEAS Sensors" in result[2]["related"]
    # Patch op is unchanged
    assert result[1] is ops[1]
