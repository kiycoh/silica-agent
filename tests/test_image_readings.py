"""ADR-0077 point 2: the chat model reads each image once; the reading stands in
for the image wherever text is indexed or shown to the agent."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from silica.kernel.recall import image_readings
from silica.kernel.text.media import figures_as_text

SPARK = '{"kind": "diagram", "description": "Architettura di Spark", "text": "Driver Program"}'


def _image(vault: Path, rel: str, data: bytes = b"\x89PNG fake") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def vault(tmp_vault):
    import silica.config
    return Path(silica.config.CONFIG.vault_path)


def test_an_image_two_notes_embed_is_read_once(vault):
    _image(vault, "Img/a.png")
    notes = [("N1", "Spark ![[a.png]]"), ("N2", "di nuovo ![](Img/a.png)")]
    calls = []

    def ask(messages):
        calls.append(messages)
        return SPARK

    assert image_readings.read_missing(notes, ask=ask) == 1
    assert image_readings.read_missing(notes, ask=ask) == 0
    assert len(calls) == 1
    parts = calls[0][-1]["content"]
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in parts)
    assert image_readings.texts_by_name()["a.png"] == "diagram: Architettura di Spark Driver Program"


def test_a_model_without_vision_stops_the_pass_at_the_first_image(vault):
    _image(vault, "a.png")
    _image(vault, "b.png", b"other bytes")
    calls = []

    def ask(messages):
        calls.append(1)
        raise RuntimeError("image input is not supported by this model")

    assert image_readings.read_missing([("N", "![[a.png]] ![[b.png]]")], ask=ask) == 0
    assert len(calls) == 1
    assert image_readings.texts_by_name() == {}


def test_the_reading_beats_the_converter_caption():
    text = "![[x.jpg]]\n\n<details>\n<summary>flowchart</summary>\n\nUser Computer\n</details>\n\ndopo"
    out = figures_as_text(text, {"x.jpg": "diagram: Spark"})
    assert "[figure: diagram: Spark]" in out and "Computer" not in out


def test_a_read_screenshot_reaches_the_embedding_text(vault):
    from silica.kernel.recall.embed import _note_text
    _image(vault, "a.png")
    image_readings.read_missing([("N", "![[a.png]]")], ask=lambda m: SPARK)
    assert "Architettura di Spark" in _note_text("Spark", "Testo ![[a.png]] fine")


def test_the_lexical_index_holds_a_reading(vault):
    from silica.kernel.recall.lexical import LexicalStore
    _image(vault, "a.png")
    image_readings.read_missing([("N", "![[a.png]]")], ask=lambda m: SPARK)
    store = LexicalStore(Path("/dev/null"))
    store.upsert("N", "N", "Testo ![[a.png]]")
    assert "spark" in store._docs["N"]


def test_read_note_shows_the_reading_under_the_embed(vault, monkeypatch):
    from silica.driver.base import NoteContent, NoteRef
    from silica.kernel.write import provenance
    from silica.tools import atomic
    _image(vault, "a.png")
    image_readings.read_missing([("N", "![[a.png]]")], ask=lambda m: SPARK)
    body = "# N\n\nPrima\n![[a.png]]\nDopo\n"
    monkeypatch.setattr(atomic, "DRIVER", SimpleNamespace(
        read_note=lambda name: NoteContent(ref=NoteRef(name=name, path="N.md"), content=body)))
    monkeypatch.setattr(provenance, "drift_map", lambda **k: {})
    out = atomic.silica_read_note("N")
    assert "![[a.png]]\n> [figure, read by " in out
    assert "Architettura di Spark" in out and out.rstrip().endswith("Dopo")


def test_bulk_embed_reads_images_first_and_embeds_raw_bodies(monkeypatch):
    from silica.kernel.recall import embed as embed_mod
    from silica.tools import graph
    body = "![](images/x.jpg)\n\n<details>\n<summary>chart</summary>\n\nY axis\n</details>\n"
    monkeypatch.setattr(graph, "DRIVER", SimpleNamespace(
        list_files=lambda folder: [SimpleNamespace(path="S.md", name="S")],
        read_note=lambda path: SimpleNamespace(content=body)))
    order = []
    monkeypatch.setattr(image_readings, "read_missing", lambda notes, **k: order.append(("read", notes)) or 0)

    class Store(list):
        _path = "embeddings.npz"

    def build(embedder, notes, **k):
        order.append(("embed", notes))
        return Store()
    monkeypatch.setattr(embed_mod, "build_index", build)
    import silica.agent.providers as providers
    monkeypatch.setattr(providers, "get_embedder", lambda cfg: object())
    graph.silica_embed_refresh()
    assert [step for step, _ in order] == ["read", "embed"]
    assert "<details>" in order[1][1][0][2]  # raw: _note_text alone decides what a figure becomes


def test_an_image_with_no_text_becomes_its_reading(vault, monkeypatch):
    from silica.sources import convert
    img = _image(vault, "Inbox/lavagna.png")

    def mineru(src, workdir):
        (workdir / "images").mkdir()
        return "", workdir / "images"
    monkeypatch.setattr(convert, "_pdf_via_mineru", mineru)
    monkeypatch.setattr(image_readings, "read_image",
                        lambda path, language, **k: {"kind": "diagram", "description": "Architettura di Spark",
                                                     "text": "", "model": "qwen3.5-9b"})
    (rel,) = convert.convert(str(img))
    note = (vault / rel).read_text(encoding="utf-8")
    assert "source: image-reading" in note and "Architettura di Spark" in note
    assert "![[lavagna.png]]" in note

    monkeypatch.setattr(image_readings, "read_image", lambda path, language, **k: None)
    with pytest.raises(ValueError, match="no readable text"):
        convert.convert(str(img))


def test_the_chat_server_gets_the_projector_beside_its_model(tmp_path):
    from silica.onboarding.serve import with_mmproj
    model = tmp_path / "Qwen3.5-9B-Q4_K_M.gguf"
    model.write_bytes(b"")
    cmd = f"llama-server -m {model} -c 8192"
    assert with_mmproj("chat", cmd) == cmd  # no projector on disk: unchanged
    proj = tmp_path / "mmproj-Qwen3.5-9B-BF16.gguf"
    proj.write_bytes(b"")
    assert with_mmproj("chat", cmd) == f"{cmd} --mmproj {proj}"
    assert with_mmproj("embeddings", cmd) == cmd
    assert with_mmproj("chat", f"{cmd} --mmproj other.gguf") == f"{cmd} --mmproj other.gguf"
