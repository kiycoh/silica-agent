"""`parse_headings` (kernel/link/ast.py): a note's YAML frontmatter is not a
heading, though its closing `---` is a setext underline to CommonMark."""
from silica.kernel.link.ast import parse_headings


def test_frontmatter_is_not_the_outline_s_first_heading():
    # silica_outline opened every note with properties with them as an H2
    # (2026-10-20): [{'level': 2, 'text': 'title: X\ntags: [a]'}, ...].
    body = "---\ntitle: X\ntags: [a]\n---\n# Heading\n\nbody\n"
    assert parse_headings(body) == [{"level": 1, "text": "Heading", "pos": body.index("# Heading")}]


def test_a_setext_heading_in_the_body_is_still_one():
    assert parse_headings("Setext\n---\n") == [{"level": 2, "text": "Setext", "pos": 0}]
    body = "---\na: 1\n---\nTitle\n---\n"
    assert parse_headings(body) == [{"level": 2, "text": "Title", "pos": body.index("Title")}]
