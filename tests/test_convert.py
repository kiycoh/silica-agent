"""convert() — non-.md → .md ingress frontier (PDF, provider-selectable).

Providers are mocked: docling/opendataloader are injected as fake modules and the
mineru subprocess is patched, so no ML models / real PDFs / installs are needed.
"""
import sys
import types
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources import convert as conv


def _inbox_note(note_rel: str) -> Path:
    return Path(CONFIG.vault_path) / note_rel


# --- dispatch ---------------------------------------------------------------

@pytest.mark.parametrize("target", ["notes.xyz", "noext", "data.csv"])
def test_unknown_extension_raises(target):
    with pytest.raises(ValueError, match="no converter"):
        conv.convert(target)


# --- shared pipeline (exercised via the docling fake) -----------------------
#
# TODO(real-api): the fakes here hand-mirror the docling / opendataloader APIs
# and the mineru CLI. They prove the SHARED pipeline, not the provider wiring —
# if a library renames those, the fakes drift with the bug and stay green. Add a
# real-install smoke test (skipif on import, one tiny bundled PDF) to catch drift.

def test_pdf_rewrites_any_image_link(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    tmp_vault.note("paper.pdf", "x")
    _fake_docling(monkeypatch, md="see [ref](https://x.test/a) and ![](a/b/fig.png)")

    body = _inbox_note(conv.convert("paper.pdf")[0]).read_text(encoding="utf-8")
    assert "[ref](https://x.test/a)" in body          # ordinary link survives
    assert "![[fig.png]]" in body                      # image link → Obsidian embed


def test_missing_file_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    with pytest.raises(ValueError, match="file not found"):
        conv.convert("ghost.pdf")


# --- docling provider (keeps figures) ---------------------------------------

def _fake_docling(monkeypatch, md="# Title\n\n![](images/fig.png)\n\nbody"):
    """Inject a fake docling whose save_as_markdown writes one image + references it."""

    class _Doc:
        def save_as_markdown(self, path, *, image_mode, artifacts_dir):
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "fig.png").write_bytes(b"\x89PNG fake")
            Path(path).write_text(md, encoding="utf-8")

    class DocumentConverter:
        def __init__(self, **kw):
            pass

        def convert(self, path):
            return types.SimpleNamespace(document=_Doc())

    dc = types.ModuleType("docling.document_converter")
    dc.DocumentConverter = DocumentConverter
    dc.PdfFormatOption = lambda **kw: None
    base = types.ModuleType("docling.datamodel.base_models")
    base.InputFormat = types.SimpleNamespace(PDF="pdf")
    popts = types.ModuleType("docling.datamodel.pipeline_options")
    popts.PdfPipelineOptions = type("PdfPipelineOptions", (), {})
    core = types.ModuleType("docling_core.types.doc")
    core.ImageRefMode = types.SimpleNamespace(REFERENCED="referenced")
    fakes = {
        "docling": types.ModuleType("docling"),
        "docling.datamodel": types.ModuleType("docling.datamodel"),
        "docling.datamodel.base_models": base,
        "docling.datamodel.pipeline_options": popts,
        "docling.document_converter": dc,
        "docling_core": types.ModuleType("docling_core"),
        "docling_core.types": types.ModuleType("docling_core.types"),
        "docling_core.types.doc": core,
    }
    for name, mod in fakes.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_pdf_docling_provider_embeds_extracted_image(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    tmp_vault.note("paper.pdf", "x")
    _fake_docling(monkeypatch)

    note_rels = conv.convert("paper.pdf", dest_dir="Concepts/X")
    assert note_rels == [f"{CONFIG.inbox_dir}/paper.md"]  # small PDF → one flat note
    body = _inbox_note(note_rels[0]).read_text(encoding="utf-8")
    assert "![[fig.png]]" in body
    assert (Path(CONFIG.vault_path) / "Concepts/X/Images/fig.png").is_file()


def test_unreferenced_extracted_image_is_not_copied(tmp_vault, monkeypatch):
    """mineru dumps every crop it detects (477 files for a 200-page book, 19
    referenced) — only images the markdown references may reach the vault."""
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    tmp_vault.note("paper.pdf", "x")
    _fake_docling(monkeypatch, md="# Title\n\nno figures referenced here")

    conv.convert("paper.pdf", dest_dir="Concepts/X")

    assert not (Path(CONFIG.vault_path) / "Concepts/X/Images/fig.png").exists()


def test_docling_missing_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    tmp_vault.note("paper.pdf", "x")
    monkeypatch.setitem(sys.modules, "docling.document_converter", None)
    with pytest.raises(ValueError, match="docling not installed"):
        conv.convert("paper.pdf")


# --- opendataloader provider (Java-backed, keeps figures) -------------------

def _fake_opendataloader(monkeypatch, md="# Title\n\n![](images/fig.png)\n\nbody"):
    """Inject a fake opendataloader_pdf.convert that writes one .md + one image."""
    mod = types.ModuleType("opendataloader_pdf")

    def convert(*, input_path, output_dir, format, image_output, image_dir):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{Path(input_path).stem}.md").write_text(md, encoding="utf-8")
        Path(image_dir).mkdir(parents=True, exist_ok=True)
        (Path(image_dir) / "fig.png").write_bytes(b"\x89PNG fake")

    mod.convert = convert
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", mod)


def test_pdf_opendataloader_provider_embeds_extracted_image(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "opendataloader")
    tmp_vault.note("paper.pdf", "x")
    _fake_opendataloader(monkeypatch)

    body = _inbox_note(conv.convert("paper.pdf", dest_dir="Concepts/X")[0]).read_text(encoding="utf-8")
    assert "![[fig.png]]" in body
    assert (Path(CONFIG.vault_path) / "Concepts/X/Images/fig.png").is_file()


def test_opendataloader_missing_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "opendataloader")
    tmp_vault.note("paper.pdf", "x")
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", None)
    with pytest.raises(ValueError, match="opendataloader-pdf not installed"):
        conv.convert("paper.pdf")


def test_opendataloader_no_markdown_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "opendataloader")
    tmp_vault.note("paper.pdf", "x")
    mod = types.ModuleType("opendataloader_pdf")
    mod.convert = lambda **kw: None  # writes nothing
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", mod)
    with pytest.raises(ValueError, match="produced no markdown"):
        conv.convert("paper.pdf")


# --- mineru provider --------------------------------------------------------

def _fake_mineru_run(returncode=0, stderr="", write_md=True):
    def run(cmd, **kw):
        if write_md:
            out = Path(cmd[cmd.index("-o") + 1])
            stem = Path(cmd[cmd.index("-p") + 1]).stem
            d = out / stem / "txt"
            (d / "images").mkdir(parents=True)
            (d / f"{stem}.md").write_text("# M\n\n![](images/h.jpg)\n", encoding="utf-8")
            (d / "images" / "h.jpg").write_bytes(b"img")

        class R:
            pass

        R.returncode, R.stderr, R.stdout = returncode, stderr, ""
        return R()

    return run


def test_pdf_mineru_provider_success(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "mineru")
    tmp_vault.note("paper.pdf", "x")
    monkeypatch.setattr(conv.subprocess, "run", _fake_mineru_run())

    body = _inbox_note(conv.convert("paper.pdf")[0]).read_text(encoding="utf-8")
    assert "![[h.jpg]]" in body
    assert (Path(CONFIG.vault_path) / "Inbox/Images/h.jpg").is_file()


def test_mineru_missing_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "mineru")
    tmp_vault.note("paper.pdf", "x")

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(conv.subprocess, "run", boom)
    with pytest.raises(ValueError, match="mineru not installed"):
        conv.convert("paper.pdf")


def test_mineru_nonzero_exit_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "mineru")
    tmp_vault.note("paper.pdf", "x")
    monkeypatch.setattr(
        conv.subprocess, "run", _fake_mineru_run(returncode=1, stderr="kaboom", write_md=False)
    )
    with pytest.raises(ValueError, match="mineru failed"):
        conv.convert("paper.pdf")


def test_unknown_provider_raises(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "pdf_provider", "bogus")
    tmp_vault.note("paper.pdf", "x")
    with pytest.raises(ValueError, match="unknown pdf_provider"):
        conv.convert("paper.pdf")


def test_respace_prose_fixes_tight_punctuation_outside_math_and_code():
    md = (
        "symmetric,and positive kernel with 10,000 samples\n"
        "$f(x,y)$ and $$\\alpha,\\beta$$ stay\n"
        "```\na,b = 1,2\n```\n"
    )
    fixed = conv._respace_prose(md)
    assert "symmetric, and positive" in fixed   # prose glitch fixed
    assert "10,000" in fixed                     # digits untouched
    assert "$f(x,y)$" in fixed                   # inline math untouched
    assert "a,b = 1,2" in fixed                  # fenced code untouched


# --- book segmentation (split_markdown) -------------------------------------

def test_split_on_headings_splits_chapters():
    # max_chars small enough that no two sections pack together → cuts land
    # exactly on the heading boundaries.
    segs = conv.split_markdown("# Book\n\nintro\n\n## One\n\naaa\n\n## Two\n\nbbb", max_chars=20)
    assert len(segs) == 3
    assert segs[0].startswith("# Book")      # preamble attached to first heading
    assert "## One" in segs[1] and "## Two" in segs[2]


def test_split_ignores_headings_inside_code_fences():
    md = "intro\n\n```\n# not a heading\n```\n\n## Real\n\nbody"
    segs = conv.split_markdown(md, max_chars=40)
    assert len(segs) == 2                     # the fenced '# ...' is not a boundary
    assert "# not a heading" in segs[0]


def test_split_packs_small_sections_together():
    """Real converters flatten everything to ## and emit lone '## Chapter N'
    lines (80-page docling probe: 53 raw segments, some 14 chars) — adjacent
    small sections must coalesce instead of becoming micro-notes."""
    md = "".join(f"## S{i}\n\n{'x' * 50}\n\n" for i in range(10))
    segs = conv.split_markdown(md, max_chars=200)
    assert 1 < len(segs) < 10                  # packed, not one-note-per-heading
    assert all(len(s) <= 200 for s in segs)
    assert segs[0].count("## S") >= 2          # a pack spans multiple headings


def test_split_small_multiheading_doc_packs_to_one():
    md = "# Paper\n\nintro\n\n## Method\n\naaa\n\n## Results\n\nbbb"
    assert conv.split_markdown(md) == [md]     # a paper stays one flat note


def test_split_dimensional_fallback_when_no_headings():
    body = "".join(f"Paragraph {i} of heading-less scanned prose.\n\n" for i in range(200))
    segs = conv.split_markdown(body, max_chars=500)
    assert len(segs) > 1                       # blind body still gets cut into parts
    assert all(len(s) <= 600 for s in segs)    # ≤ max + one paragraph of slack


def test_split_size_caps_an_oversized_heading_section():
    big = "## Huge\n\n" + "".join(f"line {i}\n\n" for i in range(300))
    segs = conv.split_markdown(big, max_chars=400)
    assert len(segs) > 1                        # a giant chapter is split further


def test_split_single_small_section_is_one_segment():
    assert conv.split_markdown("# Paper\n\nshort body") == ["# Paper\n\nshort body"]


def test_pdf_book_splits_into_multiple_inbox_notes(tmp_vault, monkeypatch):
    """A multi-chapter converted PDF becomes N inbox notes under <stem>/,
    numbered and slugged — one RECON unit per chapter, not the whole book."""
    monkeypatch.setattr(CONFIG, "pdf_provider", "docling")
    tmp_vault.note("book.pdf", "%PDF fake")
    # Two ~30k sections: front+Alpha pack into one unit, Beta overflows into
    # the next — a genuinely book-sized doc, not a paper. Varied words, not one
    # repeated char: strip_degenerate_runs would collapse a degenerate run.
    alpha = " ".join(f"alpha{i}" for i in range(4_000))
    beta = " ".join(f"beta{i}" for i in range(4_000))
    _fake_docling(monkeypatch, md=(
        f"# Book\n\nfront\n\n## Alpha\n\n{alpha}\n\n## Beta\n\n{beta}"
    ))

    paths = conv.convert("book.pdf", dest_dir="Concepts/X")

    assert len(paths) == 2
    assert paths[0] == f"{CONFIG.inbox_dir}/book/1-book.md"
    assert paths[1].endswith("2-beta.md")
    assert all((Path(CONFIG.vault_path) / p).is_file() for p in paths)
    assert "## Alpha" in (Path(CONFIG.vault_path) / paths[0]).read_text(encoding="utf-8")
    assert "## Beta" in (Path(CONFIG.vault_path) / paths[1]).read_text(encoding="utf-8")
