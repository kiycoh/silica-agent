from silica.kernel.base.markdown import extract_links

def test_extract_links():
    content = """
    Check this [[Neural Network]] and [[Concepts#Details|Concepts spoke]].
    
    But ignore this code block:
    ```
    [[Neural Network]] inside code block
    ```
    
    And ignore inline code `[[Concepts]]` inside it.
    
    Also ignore embeds like ![[image.png]] and ![[Attachment.pdf]].
    
    But keep [[Spoke Note]].
    """
    targets = extract_links(content)
    assert targets == ["Neural Network", "Concepts", "Spoke Note"]


def test_intra_note_anchors_are_not_note_links():
    # [[#Heading]] and [[^block]] point inside the note that carries them.
    # Returning them made every one an "unresolved link" no resolver could
    # ever satisfy, and the graph regression gate rolled back the chunk.
    content = "See [[#Derivazione]] and [[^ab12cd]], but keep [[Vera Nota]]."
    assert extract_links(content) == ["Vera Nota"]


def test_extract_links_typed_splits_scaffold_from_prose():
    from silica.kernel.base.markdown import extract_links, extract_links_typed
    content = (
        "---\n"
        "parent note: \"[[Hub]]\"\n"
        "related:\n  - \"[[Hub]]\"\n  - \"[[Sibling]]\"\n"
        "---\n\n"
        "# Title\n\n"
        "## [[Spoke]]\n\n"
        "Prose mentions [[Sibling]] and [[Other]].\n"
    )
    typed = extract_links_typed(content)
    assert list(typed) == extract_links(content)          # same targets, same order
    assert typed == {"Hub": True, "Sibling": False, "Spoke": True, "Other": False}
    assert extract_links_typed("no links here") == {}


def test_extract_links_typed_prose_wins_over_a_scaffold_mention():
    from silica.kernel.base.markdown import extract_links_typed
    content = "---\nrelated:\n  - \"[[X]]\"\n---\n\nBody cites [[X]] in a sentence.\n"
    assert extract_links_typed(content) == {"X": False}


def test_adr_prose_reference_is_a_link():
    # 36 ADRs cite each other as "ADR-0001" in prose and had out_links: [].
    # The ADR corpus gets its graph from the reference it already writes.
    content = "Guardrail (keeps ADR-0001 intact); see ADR-0029.\nNot in `ADR-0002` code.\n"
    assert extract_links(content) == ["ADR-0001", "ADR-0029"]


def test_a_code_span_in_an_alias_is_still_a_link():
    from silica.kernel.base.markdown import extract_links

    assert extract_links("Use [[Undo and revert|`/undo`]] now.") == ["Undo and revert"]
    assert extract_links("[[A|`x`]] then [[B|`y`]]") == ["A", "B"]


def test_a_code_span_that_only_shows_a_link_stays_code():
    from silica.kernel.base.markdown import extract_links

    assert extract_links("`[[A|b]]` and `[[C|`") == []
    assert extract_links("```\n[[A|`x`]]\n```\n") == []
    assert extract_links("[[A| `x` never closed") == []


def test_the_reader_gets_a_link_and_not_the_brackets():
    from silica.ui.web.server import _linkify

    html = _linkify("Use [[Undo and revert|`/undo`]] now.", lambda name: name + ".md")
    assert "[[" not in html and "note-link" in html and "/undo" in html
