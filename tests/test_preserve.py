# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""CI holds the composers to the preservation rule (ADR-0093 decision 3).

The nineteen composers behind the driver's writes run, each under the Loss
its call site declares, over committed synthetic notes shaped like the ones
that tripped them on real vaults (2026-10-11): a one-line `related:` (null,
`[]`, empty, a link), a flush-left list, unparseable YAML with `sources:`, a
first body line indented by one space, an alias with a leading space after a
word, a table row autolink reaches, footnotes, a CRLF note. No real vault note
is committed. The second half checks the rule still catches the corruptions
of the Khoj counterexample on the same notes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.kernel.link.autolink import build_title_index
from silica.kernel.link.health.preservation import COMPOSERS, preservation
from silica.kernel.write import frontmatter
from silica.kernel.write.preserve import SYSTEM_KEYS, lost

FIXTURES = Path(__file__).parent / "fixtures" / "preserve"
NOTES = sorted(FIXTURES.glob("*.md"))
TITLES = build_title_index([p.stem for p in NOTES]
                           + ["Machine learning", "Statistics", "Efficienza asintotica"])


def _read(p: Path) -> tuple[str, str]:
    """(the bytes as text, what the fs backend's read hands a composer)."""
    raw = p.read_bytes().decode("utf-8")
    return raw, raw.replace("\r\n", "\n").replace("\r", "\n")


def test_the_fixtures_hold_the_shapes_that_tripped():
    assert len(COMPOSERS) == 19
    raw = {p.stem: p.read_bytes() for p in NOTES}
    assert b"\r\n" in raw["crlf"]
    assert frontmatter.split(raw["unparseable sources"].decode())[0] is None
    for stem, line in [("related null", b"related: null\n"), ("related empty list", b"related: []\n"),
                       ("related empty", b"related:\nstatus"), ("related one link", b'related: "[[')]:
        assert line in raw[stem], stem


@pytest.mark.parametrize("name", list(COMPOSERS))
@pytest.mark.parametrize("note", NOTES, ids=lambda p: p.stem)
def test_no_composer_loses_a_line_or_a_value(note, name):
    raw, text = _read(note)
    fn, loss = COMPOSERS[name]
    after = fn(note.name, text, {"stem": note.stem, "titles": TITLES})
    assert lost(raw, after, loss=loss) is None


def test_t5_reads_clean_on_every_fixture():
    for note in NOTES:
        raw, text = _read(note)
        assert preservation(note.name, text, note.stem, TITLES) == {}, note.stem


# ---------------------------------------------------------------- the rule still catches

def _split_head(t: str) -> tuple[str, str]:
    _d, raw, body = frontmatter.split(t)
    return (t[: len(t) - len(body)] if raw is not None else ""), body


def dashes(t):  # Khoj interact_with_files.ts:630, applied to the whole file
    return [t.replace("—", "-").replace("–", "-")]


def highlights(t):  # Khoj chat_view.ts:2312, every == of the file
    return [t.replace("==", "")]


def drop_line(t):  # every non-blank body line, dropped in turn
    head, body = _split_head(t)
    lines = body.split("\n")
    return [head + "\n".join(lines[:i] + lines[i + 1:])
            for i, line in enumerate(lines) if line.strip()]


def drop_key(t):  # every key the rule keeps, dropped in turn
    data, _raw, body = frontmatter.split(t)
    if not isinstance(data, dict):
        return []
    out = []
    for k, v in data.items():
        if k in SYSTEM_KEYS or v in (None, "", [], {}):
            continue
        rest = {kk: vv for kk, vv in data.items() if kk != k}
        out.append(frontmatter.dump(rest, body))
    return out


def _wrap(a, b):
    def fn(t):
        head, body = _split_head(t)
        return [head + a + "\n" + body.rstrip("\n") + "\n" + b + "\n"] if body.strip() else []
    fn.__name__ = f"wrap {a}"
    return fn


@pytest.mark.parametrize("corrupt", [dashes, highlights, drop_line, drop_key,
                                     _wrap("```", "```"), _wrap("%%", "%%"),
                                     _wrap("<!--", "-->")],
                         ids=lambda f: f.__name__)
def test_the_rule_catches_every_corruption_that_changed_a_note(corrupt):
    changed = 0
    for note in NOTES:
        _raw, text = _read(note)
        for after in corrupt(text):
            if after == text:
                continue
            changed += 1
            assert lost(text, after) is not None, (note.stem, after)
    assert changed, "no fixture exercises this corruption"


def test_t5_joins_the_integrity_rate(tmp_path, monkeypatch):
    import shutil

    from silica.kernel.link.health import integrity_probe, preservation as pv

    for name in ("human mixed.md", "related null.md"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    res = integrity_probe(tmp_path)
    assert (res["rate"], res["preservation_notes_losing"]) == (1.0, 0)

    def lossy(rel, text, ctx):  # a composer that starts dropping the last line
        return text.rstrip("\n").rsplit("\n", 1)[0] + "\n"

    monkeypatch.setitem(pv.COMPOSERS, "moc", (lossy, None))
    res = integrity_probe(tmp_path)
    assert res["rate"] == 0.0 and res["preservation_notes_losing"] == 2
