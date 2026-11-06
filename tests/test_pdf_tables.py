"""A PDF's tables read back as tables (ADR-0087 amendment 2026-11-06).

The PDFs are real and built by tests/doc_factory.py, so PDFium, its character
boxes and its lock are the real ones. The layout model and the chat model are
fakes: no network, no weights, no paid call.
"""
from __future__ import annotations

import copy
import hashlib
import math
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from silica.sources import convert as conv
from silica.sources import pdf_extract, pdf_tables
from tests.doc_factory import pdf_bytes

# Bound at import: conftest's autouse fixture switches this off for every test.
_REAL_WEIGHTS = pdf_tables._weights

PAGE = ("Opening line of the running text here.\nName Count\nAlpha 12\nBeta 34\n"
        "Closing line of the running text here.")
# doc_factory's lines sit at 720, 702.4, 684.8...: this holds the middle three.
BOX = (60.0, 661.0, 300.0, 714.0)
TABLE = "| Name | Count |\n| --- | --- |\n| Alpha | 12 |\n| Beta | 34 |"


def _rows(boxes: list[tuple], width: float = 612.0, height: float = 792.0) -> np.ndarray:
    """What the model returns for `boxes` in PDF points on a page of that size,
    among a low score and another class that must not count."""
    scale = 1024 / max(width, height)
    dx = (1024 - math.ceil(width * scale)) // 2
    dy = (1024 - math.ceil(height * scale)) // 2
    out = np.zeros((1, 300, 6), np.float32)
    for k, (left, bottom, right, top) in enumerate(boxes):
        out[0, k] = [left * scale + dx, (height - top) * scale + dy,
                     right * scale + dx, (height - bottom) * scale + dy, 0.9, 5]
    out[0, 298] = [0, 0, 1024, 1024, 0.1, 5]
    out[0, 299] = [0, 0, 1024, 1024, 0.9, 1]
    return out


class _Session:
    def __init__(self, boxes: list[tuple]):
        self.boxes = boxes
        self.runs = 0
        self.locked: list[bool] = []

    def run(self, _outputs, feed):
        image = feed["images"]
        assert image.shape == (1, 3, 1024, 1024) and image.dtype == np.float32
        assert image.max() <= 1.0
        self.runs += 1
        self.locked.append(pdf_extract._PDFIUM.locked())
        return [_rows(self.boxes)]


class _Model:
    """get_provider's fake: `replies` in turn, an Exception raised, a tuple
    (text, finish_reason) stopped there; the last one repeats."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.locked: list[bool] = []
        self.guard = threading.Lock()

    def call_llm(self, **kw):
        with self.guard:
            self.calls.append(kw)
            self.locked.append(pdf_extract._PDFIUM.locked())
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        text, finish = reply if isinstance(reply, tuple) else (reply, "stop")
        return SimpleNamespace(text=text, finish_reason=finish)


@pytest.fixture(autouse=True)
def _setting(monkeypatch) -> None:
    monkeypatch.setenv("SILICA_PDF_TABLES", "1")


def test_off_unless_the_setting_says_so(monkeypatch):
    """[stt] brings onnxruntime too: being importable must not fetch or call anything."""
    monkeypatch.delenv("SILICA_PDF_TABLES")
    monkeypatch.setattr(pdf_tables, "_session", lambda: pytest.fail("loaded without the setting"))
    data = {"pages": [[["text", 10.0]]], "no_text": [], "math_read": []}
    assert pdf_tables.stitched(Path("x.pdf"), data) is data["pages"]


class _Path:
    def __init__(self, bounds):
        import pypdfium2.raw as raw
        self.type, self.bounds = raw.FPDF_PAGEOBJ_PATH, bounds

    def get_bounds(self):
        return self.bounds


class _Page:
    def __init__(self, paths):
        self.paths, self.closed = paths, False

    def get_size(self):
        return 600.0, 800.0

    def get_objects(self, max_depth):
        return iter(self.paths)

    def close(self):
        self.closed = True


ACROSS = (50, 700, 550, 701)
DOWN = (100, 300, 101, 500)
BLOCK = (50, 300, 550, 500)


@pytest.mark.parametrize("paths, expected", [
    ([], False),
    ([ACROSS, ACROSS], False),            # a header and a footer rule
    ([ACROSS] * 3, True),
    ([DOWN], False),
    ([DOWN] * 2, True),
    ([BLOCK] * 9, False),                 # filled shapes are not rules
    ([(50, 700, 60, 701)] * 9, False),    # a rule under one word
])
def test_thin_rules_are_a_sign_of_a_table(paths, expected):
    page = _Page([_Path(p) for p in paths])
    assert pdf_tables._signs_of_table({0: page}, 0, [["Running text", 10.0]]) is expected
    assert page.closed


@pytest.mark.parametrize("line, expected", [
    ("Table 3. Results", True),
    ("Tabella 2: importi", True),
    ("Tab. IV", True),
    ("The table 3 shows", False),         # in a sentence, not a caption
    ("Tableware 3", False),
])
def test_a_caption_is_a_sign_of_a_table(line, expected):
    assert pdf_tables._signs_of_table({0: _Page([])}, 0, [[line, 10.0]]) is expected


def test_a_page_past_the_object_cap_stops_counting(monkeypatch):
    monkeypatch.setattr(pdf_tables, "_MAX_OBJECTS", 2)
    page = _Page([_Path(BLOCK), _Path(BLOCK), _Path(ACROSS), _Path(ACROSS), _Path(ACROSS)])
    assert pdf_tables._signs_of_table({0: page}, 0, []) is False


def test_a_page_with_no_sign_is_not_drawn(monkeypatch, tmp_path):
    session = _on(monkeypatch, tmp_path, _Model())
    monkeypatch.setattr(pdf_tables, "_signs_of_table", lambda doc, i, prod: False)
    monkeypatch.setattr(pdf_tables, "_draw", lambda doc, i: pytest.fail("drew a page with no sign"))
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr("pypdfium2.PdfDocument", lambda path: SimpleNamespace(close=lambda: None))
    data = {"pages": [[["text", 10.0]]], "no_text": [], "math_read": []}
    assert pdf_tables.stitched(src, data) is data["pages"]
    assert session


def test_a_number_cut_from_a_longer_one_is_refused():
    table = "| a | b |\n| --- | --- |\n| x | 20 |"
    assert pdf_tables._accepted(table, "stop", "a b\nx 2024") is None
    assert pdf_tables._accepted(table, "stop", "a b\nx 20") == table


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    monkeypatch.delenv("SILICA_PDF_VISION_PAGES", raising=False)
    pdf_extract._CACHE.clear()
    return tmp_path


def _on(monkeypatch, tmp_path, model: _Model, boxes: list[tuple] | None = None) -> _Session:
    """The step switched on, through a fake onnxruntime so `_session` itself runs."""
    session = _Session([BOX] if boxes is None else boxes)
    weights = tmp_path / "layout.onnx"
    weights.write_bytes(b"weights")
    options = SimpleNamespace()

    def inference(path, opts, providers):
        assert path == str(weights) and opts is options
        assert providers == ["CPUExecutionProvider"]
        assert (opts.intra_op_num_threads, opts.inter_op_num_threads) == (2, 1)
        assert opts.enable_cpu_mem_arena is False
        return session

    fake = types.ModuleType("onnxruntime")
    fake.SessionOptions = lambda: options
    fake.InferenceSession = inference
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setattr(pdf_tables, "_weights", lambda: weights)
    monkeypatch.setattr(pdf_tables, "_signs_of_table", lambda doc, i, prod: True)

    def provider(config, role="router"):
        assert role == "router"
        return model

    monkeypatch.setattr("silica.agent.providers.get_provider", provider)
    return session


def _data(tmp_path: Path, pages: list[str], **over) -> tuple[Path, dict]:
    src = tmp_path / "doc.pdf"
    src.write_bytes(pdf_bytes(pages))
    return src, {**pdf_extract.read(src), "math_read": [], **over}


def _texts(page: list) -> list[str]:
    return [t for t, _ in page]


# --- when it does nothing -----------------------------------------------------

def test_hosted_is_a_no_op(local, monkeypatch):
    model = _Model(TABLE)
    session = _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE])
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert pdf_tables.stitched(src, data) is data["pages"]
    assert session.runs == 0 and not model.calls


def test_missing_onnxruntime_is_a_no_op_and_fetches_nothing(local, monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setattr(pdf_tables, "_weights", lambda: pytest.fail("fetched without onnxruntime"))
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_no_weights_is_a_no_op(local, monkeypatch):
    _on(monkeypatch, local, _Model(TABLE))
    monkeypatch.setattr(pdf_tables, "_weights", lambda: None)
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_weights_that_will_not_load_are_a_no_op(local, monkeypatch):
    _on(monkeypatch, local, _Model(TABLE))

    def refuse(*a, **k):
        raise RuntimeError("protobuf parsing failed")

    monkeypatch.setattr(sys.modules["onnxruntime"], "InferenceSession", refuse)
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_nothing_is_loaded_when_no_page_is_a_candidate(local, monkeypatch):
    monkeypatch.setattr(pdf_tables, "_session", lambda: pytest.fail("loaded for nothing"))
    src, data = _data(local, [PAGE], math_read=[0])
    assert pdf_tables.stitched(src, data) is data["pages"]


# --- the weights --------------------------------------------------------------

class _Download:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, size):
        yield self.body[:3]
        yield self.body[3:]


def _home() -> Path:
    from silica.kernel.recall import paths
    return paths._SILICA_HOME / "models"


def test_a_hash_mismatch_refuses_and_leaves_nothing(monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Download(b"not the weights"))
    assert _REAL_WEIGHTS() is None
    assert list(_home().iterdir()) == []


def test_weights_land_once_and_the_redirect_is_followed(monkeypatch):
    body = b"the weights"
    seen: list[dict] = []

    def stream(method, url, **kw):
        seen.append(kw)
        return _Download(body)

    monkeypatch.setattr(pdf_tables, "_WEIGHTS_SHA256", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(httpx, "stream", stream)
    target = _REAL_WEIGHTS()
    assert target == _home() / "layout.onnx" and target.read_bytes() == body
    assert [p.name for p in _home().iterdir()] == ["layout.onnx"]
    assert seen[0]["follow_redirects"] is True
    assert _REAL_WEIGHTS() == target and len(seen) == 1


def test_offline_counts_as_unavailable(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "stream", down)
    assert _REAL_WEIGHTS() is None
    assert list(_home().iterdir()) == []


# --- the reading --------------------------------------------------------------

def test_a_tables_characters_are_replaced_at_the_first_one(local, monkeypatch):
    model = _Model(TABLE)
    _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE])
    pages = pdf_tables.stitched(src, data)
    assert _texts(pages[0]) == ["Opening line of the running text here.", f"\n{TABLE}\n",
                                "Closing line of the running text here."]
    (call,) = model.calls
    assert (call["max_tokens"], call["temperature"], call["reasoning"]) == (2500, 0.0, False)
    prompt = call["messages"][0]["content"]
    assert "Name" in prompt and "Alpha" in prompt and "34" in prompt
    assert "Opening" not in prompt and "Closing" not in prompt


def test_the_line_after_a_table_is_not_read_as_a_row(local, monkeypatch):
    from markdown_it import MarkdownIt

    _on(monkeypatch, local, _Model(TABLE))
    src, _ = _data(local, [PAGE])
    md, _images = conv._via_pdfium(src, local)
    html = MarkdownIt("commonmark").enable("table").render(md)
    assert "<td>Beta</td>" in html
    assert "<p>Closing line of the running text here.</p>" in html


def test_a_short_table_at_a_titles_size_is_no_heading():
    body = "Running text long enough to set the size of the body of this page."
    table = "| Name |\n| --- |\n| Alpha |"
    lines = [[(ch, k) for k, ch in enumerate(body)], [(ch, 100 + k) for k, ch in enumerate("Name")]]
    page = pdf_tables._assembled([[body, 11.0], ["Name", 14.0]], lines,
                                 {100 + k: 0 for k in range(4)}, {0: table})
    md = conv._pdf_markdown([page], [])
    assert "#" not in md and table in md


def test_only_a_read_tables_characters_leave_and_it_stands_mid_line():
    text = "Before Name After Kept"
    line = [(ch, k) for k, ch in enumerate(text)]
    owner = {**{k: 0 for k in range(7, 11)}, **{k: 1 for k in range(18, 22)}}
    page = pdf_tables._assembled([[text, 11.0]], [line], owner, {0: "| Name |"})
    assert page == [["Before \n| Name |\n After Kept", 0.0]]


def test_a_veto_keeps_the_text(local, monkeypatch):
    _on(monkeypatch, local, _Model("NOT TABLE"))
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_an_answer_without_rows_keeps_the_text(local, monkeypatch):
    _on(monkeypatch, local, _Model("Name Count Alpha 12 Beta 34"))
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_an_invented_digit_keeps_the_text(local, monkeypatch):
    _on(monkeypatch, local, _Model(TABLE.replace("34", "35")))
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_a_length_stop_keeps_the_text(local, monkeypatch):
    _on(monkeypatch, local, _Model((TABLE, "length")))
    src, data = _data(local, [PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]


def test_a_row_wider_than_its_header_keeps_the_text():
    ragged = "| N | h |\n| --- | --- |\n| base | 6 | 8 |"
    assert pdf_tables._accepted(ragged, "stop", "N h base 6 8") is None
    even = "| | N | h |\n| --- | --- | --- |\n| base | 6 | 8 |"
    assert pdf_tables._accepted(even, "stop", "N h base 6 8") == even
    escaped = "| a | b |\n| --- | --- |\n| x\\|y | 1 |"
    assert pdf_tables._accepted(escaped, "stop", "a b x|y 1") == escaped


def test_rowspan_is_not_a_printed_digit():
    html = '<table><tr><td rowspan="7">Alpha</td><td>12</td></tr></table>'
    assert pdf_tables._accepted(html, "stop", "Alpha 12") == html
    assert pdf_tables._accepted(html.replace("12", "13"), "stop", "Alpha 12") is None


def test_a_fence_is_stripped_and_tabs_become_a_table():
    fenced = f"```markdown\n{TABLE}\n```"
    assert pdf_tables._accepted(fenced, "stop", "Alpha 12 Beta 34") == TABLE
    tabbed = "Name\tCount\nAlpha\t12\nBeta"
    assert pdf_tables._accepted(tabbed, "stop", "Alpha 12") == (
        "| Name | Count |\n| --- | --- |\n| Alpha | 12 |\n| Beta |  |")


def test_a_first_call_failure_keeps_everything_and_sends_no_other(local, monkeypatch):
    model = _Model(RuntimeError("no model configured"))
    _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE, PAGE, PAGE])
    assert pdf_tables.stitched(src, data) is data["pages"]
    assert len(model.calls) == 1


def test_a_later_failure_keeps_that_table_alone(local, monkeypatch):
    model = _Model(TABLE, RuntimeError("timed out"), TABLE)
    _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE, PAGE, PAGE])
    pages = pdf_tables.stitched(src, data)
    assert len(model.calls) == 3
    changed = [i for i in range(3) if pages[i] != data["pages"][i]]
    assert len(changed) == 2 and changed[0] == 0


def test_math_read_and_no_text_pages_are_skipped(local, monkeypatch):
    model = _Model(TABLE)
    session = _on(monkeypatch, local, model)
    src, data = _data(local, ["", PAGE, PAGE], math_read=[1])
    assert data["no_text"] == [0]
    pages = pdf_tables.stitched(src, data)
    assert session.runs == 1 and len(model.calls) == 1
    assert pages[0] is data["pages"][0] and pages[1] is data["pages"][1]
    assert pages[2] is not data["pages"][2]


def test_data_is_not_mutated(local, monkeypatch):
    _on(monkeypatch, local, _Model(TABLE))
    src, data = _data(local, [PAGE, PAGE])
    before = copy.deepcopy(data)
    pages = pdf_tables.stitched(src, data)
    assert pages is not data["pages"] and pages[0] is not data["pages"][0]
    assert data == before


def test_a_page_whose_lines_differ_is_left_alone(local, monkeypatch):
    model = _Model(TABLE)
    _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE])
    data["pages"] = [[["Other words than the page's own", 11.0]]]
    assert pdf_tables.stitched(src, data) is data["pages"]
    assert not model.calls


def test_a_page_that_will_not_draw_keeps_its_text_and_the_next_is_read(local, monkeypatch):
    model = _Model(TABLE)
    session = _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE, PAGE])
    real = pdf_tables._draw

    def draw(doc, i):
        if i == 0:
            raise ValueError("MediaBox of zero size")
        return real(doc, i)

    monkeypatch.setattr(pdf_tables, "_draw", draw)
    pages = pdf_tables.stitched(src, data)
    assert session.runs == 1
    assert pages[0] is data["pages"][0] and pages[1] is not data["pages"][1]


def test_a_file_gone_since_the_read_keeps_the_text(local, monkeypatch):
    model = _Model(TABLE)
    _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE])
    src.unlink()
    assert pdf_tables.stitched(src, data) is data["pages"]
    assert not model.calls


def test_pdfium_is_on_its_lock_and_the_slow_parts_are_off_it(local, monkeypatch):
    model = _Model(TABLE)
    session = _on(monkeypatch, local, model)
    src, data = _data(local, [PAGE, PAGE])
    seen: list[bool] = []
    for name in ("_draw", "_chars"):
        real = getattr(pdf_tables, name)

        def held(*a, real=real):
            seen.append(pdf_extract._PDFIUM.locked())
            return real(*a)

        monkeypatch.setattr(pdf_tables, name, held)
    pdf_tables.stitched(src, data)
    assert seen == [True] * 4
    assert session.locked == [False, False] and model.locked == [False, False]
    assert not pdf_extract._PDFIUM.locked()


def test_progress_is_reported_on_the_calling_thread(local, monkeypatch):
    _on(monkeypatch, local, _Model(TABLE))
    src, data = _data(local, [PAGE, PAGE])
    seen: list[tuple] = []
    caller = threading.current_thread()

    def report(done, total):
        assert threading.current_thread() is caller
        seen.append((done, total))

    token = conv.PAGE_PROGRESS.set(report)
    try:
        pdf_tables.stitched(src, data)
    finally:
        conv.PAGE_PROGRESS.reset(token)
    assert seen == [(0, 2), (1, 2), (2, 2), (0, 2), (1, 2), (2, 2)]


# --- the pieces ---------------------------------------------------------------

def _word(text: str, x: float, y: float, widths: dict[str, float]) -> list[tuple]:
    out = []
    for ch in text:
        w = widths.get(ch, 5.0)
        out.append((ch, (x, y, x + w, y + 10.0)))
        x += w
    return out


def test_layout_text_keeps_words_whole_with_a_proportional_font():
    """Padding to a fixed column broke "Occurrence" into "Occu rrence": a wide
    letter puts the next one past its median-width column."""
    wide = {"O": 9.0, "c": 5.0, "u": 6.0, "r": 3.0, "e": 5.0, "n": 6.0}
    cells = _word("Occurrence", 10.0, 100.0, wide) + _word("Count", 150.0, 100.0, wide) \
        + _word("Rare", 10.0, 80.0, wide) + _word("7", 150.0, 80.0, wide)
    rows = pdf_tables._layout_text(cells).splitlines()
    assert rows[0].split() == ["Occurrence", "Count"]
    assert rows[1].split() == ["Rare", "7"]
    assert rows[0].index("Count") == rows[1].index("7")


def test_layout_text_of_nothing_is_empty():
    assert pdf_tables._layout_text([(" ", (0, 0, 1, 1)), ("a", None)]) == ""


def test_nms_keeps_the_better_of_an_overlap_and_what_is_apart():
    best = [0.0, 0.0, 100.0, 100.0, 0.9, 5.0]
    overlap = [5.0, 5.0, 100.0, 100.0, 0.5, 5.0]
    touching = [60.0, 0.0, 160.0, 100.0, 0.8, 5.0]  # IoU 0.25, under 0.45
    apart = [200.0, 200.0, 300.0, 300.0, 0.3, 5.0]
    assert pdf_tables._nms([overlap, apart, best, touching]) == [best, touching, apart]


def test_a_box_maps_back_to_pdf_points():
    session = _Session([BOX])
    got = pdf_tables._detect(session, np.zeros((1024, 1024, 3), np.uint8), 1024 / 792,
                             116, 0, 792.0)
    assert len(got) == 1 and got[0] == pytest.approx(BOX, abs=0.01)
