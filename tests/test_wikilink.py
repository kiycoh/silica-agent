from silica.kernel.link.ast import extract_links

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
