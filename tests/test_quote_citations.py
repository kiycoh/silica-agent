# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A claim keeps its own source through /nucleate (ADR-0158 point 9).

A deep research's part cites the banked quote each sentence stands on, `[Q3]`,
and says so with `citations: quotes`. The outline lane is told to keep the
markers on the lines a model writes, and CLEANUP keys each one to the leaf's
bank as a footnote carrying the quote, its URL and the leaf. Everything else,
today's /web-search notes first, nucleates as it did: point 12's gate compares
the two, so arm A is pinned byte for byte.

The model is stood in for (`outline._default_ask`); no call leaves the process.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from silica.config import CONFIG
from silica.kernel import forms
from silica.kernel import outline as ol
from silica.kernel.write import provenance as pv
from silica.sources import web_research as wr

FIXTURE = Path(__file__).parent / "fixtures" / "arm_a_nucleate.txt"

BANK = {
    "Q1": wr.Quote(url="https://example.org/cells", quote="Mitochondria make most of the ATP a cell uses.", why="what they do"),
    "Q2": wr.Quote(url="https://example.org/genes", quote="Mitochondrial DNA is passed down from the mother alone.", why="inheritance"),
    "Q3": wr.Quote(url="https://example.org/cells", quote="A liver cell holds up to two thousand mitochondria.", why="how many"),
}
BODY = ("## What they do\n\nMitochondria produce most of the energy a cell spends, as ATP [Q1]. "
        "A liver cell can hold as many as two thousand of them [Q3].\n\n"
        "## Inheritance\n\nMitochondrial DNA is passed down from the mother alone.\n"
        "Fathers contribute none of it [Q2, Q9].\n")
TRACE = ['[{"url": "https://example.org/cells", "title": "Cells"}, {"url": "https://example.org/genes", "title": "Genes"}]',
         "Source: https://example.org/cells\nMitochondria make most of the ATP a cell uses. A liver cell holds up to two thousand mitochondria.",
         "Source: https://example.org/genes\nMitochondrial DNA is passed down from the mother alone."]


def _outline(claims: tuple[str, str]) -> dict:
    return {"lesson_title": "Mitochondria", "spine": ["Cell energy", "Maternal inheritance"],
            "ideas": [{"title": "Cell energy", "section": "What they do", "claim": claims[0], "depends_on": []},
                      {"title": "Maternal inheritance", "section": "Inheritance", "claim": claims[1],
                       "depends_on": [{"title": "Cell energy", "relation": "contrasts",
                                       "why": "one is about energy, the other about genes"}]}]}


def _ask(seen: list, claims: tuple[str, str]):
    def ask(system, user, *, max_tokens, **kw):
        seen.append((system, user))
        if system.startswith(ol.STAGE_A_TAG):
            return _outline(claims)
        return {"edges": []}
    return ask


def _write(concept: str, body: str, bank: dict, trace: list[str], *, quote_ids: bool, folder: str = "") -> str:
    """A findings note and its leaf, as web_research writes them after its loop."""
    from silica.driver import DRIVER

    bound, sources, audit = wr.bind_citations(body, wr._collect_sources(trace), bank, quote_ids=quote_ids)
    rel = wr.unique_inbox_path(concept, folder=folder)
    DRIVER.create(rel, wr.build_note(concept, bound, sources, lanes=audit, cites_quotes=quote_ids))
    wr.write_leaf(rel, trace, bank=dict(bank))
    return rel


def _nucleate(monkeypatch, files: list[str], ask) -> Any:
    """A /nucleate as the lane runs it by default: looked-up bodies, no cached
    prefix, whatever the developer's environment says. The Coordinator's work
    queue is a list here: what it would hand the workers is seen, not run."""
    import types

    from silica.router.orchestrator import InjectorFSM
    from silica.router.warning_ledger import WarningLedger

    monkeypatch.setattr("silica.config.CONFIG.nucleate_lane", "outline")
    monkeypatch.delenv("SILICA_OUTLINE_BODIES", raising=False)
    monkeypatch.delenv("SILICA_OUTLINE_CACHE", raising=False)
    monkeypatch.setattr(ol, "_default_ask", ask)
    fsm: Any = InjectorFSM(inbox_files=files, target_dir="Research")
    fsm.warning_ledger = WarningLedger()
    queued: list = []
    fsm.work_queue = types.SimpleNamespace(enqueue=queued.append)
    fsm.queued = queued
    fsm.run()
    return fsm


def _read(rel: str) -> str:
    return (Path(CONFIG.vault_path) / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------- arm A, pinned


def test_a_web_search_note_nucleates_byte_for_byte_as_before(tmp_vault, monkeypatch):
    """Point 12 compares today's /web-search + /nucleate (arm A) against the
    deep research. The fixture was written by the code before point 9 (on
    502385e), so a change here moves arm A and voids that comparison: update it
    only once the gate has run or the owner has said arm A may move."""
    seen: list = []
    rel = _write("What do mitochondria do?", BODY, BANK, TRACE, quote_ids=False)
    fsm = _nucleate(monkeypatch, [rel], _ask(seen, ("Mitochondria make the ATP a cell spends.",
                                                    "Mitochondrial DNA comes from the mother.")))

    # The prompt is the stage's own constant, with nothing appended.
    assert [s for s, _ in seen] == [ol.STAGE_A.format(lang="English", rels=", ".join(ol.RELATIONS))]
    vault = Path(CONFIG.vault_path)
    blob = [f"##### ask {s.splitlines()[0]}\n{u}" for s, u in seen]
    blob += [f"##### {p.relative_to(vault)}\n{p.read_text(encoding='utf-8')}" for p in sorted(vault.rglob("*.md"))]
    got = re.sub(r"run [0-9a-f]{8}", "run <id>", re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", "\n".join(blob)))
    assert got == FIXTURE.read_text(encoding="utf-8")
    # A thin note still goes to the expand worker, as before point 9.
    assert "expand" in {item.kind for item in fsm.queued}


def test_only_a_part_written_with_quote_ids_carries_the_stamp(tmp_vault):
    plain = wr.build_note("Q", "Body [1].", [("https://a.example", "A")])
    part = wr.build_note("Q", "Body [Q1].", [("https://a.example", "A")], cites_quotes=True)

    assert "citations:" not in plain and not forms.cites_quotes(plain)
    assert "form:" not in plain and forms.stamped_form(plain) == ""
    assert "\nform: study\ncitations: quotes\n" in part and forms.cites_quotes(part)
    # The part's form is stamped, so its lens and lane are the stamp's.
    assert forms.resolve(part) == forms.Form("study", "default", "stamp")


def _dispatch(files: list[str], vote: str) -> tuple[dict, list[str]]:
    """/nucleate's dispatch seam (ingest.file_drafts) on a folder whose sniff
    says `vote` for every unstamped file: each file's verdict as printed, and
    the files left to distill."""
    import types

    from silica.sources import ingest

    printed: list[str] = []
    kept = ingest.file_drafts(files, "Research", None, types.SimpleNamespace(print=printed.append))
    verdicts = {f: line.split("(")[-1].rstrip(")") for line in printed for f in files if f"{f}:" in line}
    return verdicts, kept


@pytest.mark.parametrize("vote", ["clip", "draft"])
def test_a_part_resolves_by_its_stamp_whatever_the_folder_votes(tmp_vault, monkeypatch, vote):
    """Live, 2026-11-12: the folder vote gave two parts the clip lens. The lane
    stayed the outline (a sniff never picks it), but the lens was the sniff's,
    and a draft vote files a part intact, never distilled. Stamped, a part is
    study whatever the vote; an unstamped sibling follows the vote as today."""
    monkeypatch.setattr(forms, "sniff_form", lambda text: vote)
    one = _write("Part one", "## A\n\nOne claim [Q1].\n", BANK, TRACE, quote_ids=True, folder="topic")
    two = _write("Part two", "## B\n\nTwo claim [Q2].\n", BANK, TRACE, quote_ids=True, folder="topic")
    tmp_vault.note("Inbox/topic/Loose notes.md", "# Loose\n\nSomething the person saved.\n")
    sibling = "Inbox/topic/Loose notes.md"

    verdicts, kept = _dispatch([one, two, sibling], vote)

    assert verdicts[one] == verdicts[two] == "stamp"
    assert one in kept and two in kept
    if vote == "clip":
        assert verdicts[sibling] == "folder vote" and sibling in kept
    else:
        assert sibling not in kept  # filed as the person's draft, as before


def test_a_part_under_a_clip_vote_gets_the_outline_lane_and_the_instruction(tmp_vault, monkeypatch):
    rel = _write("What do mitochondria do?", BODY, BANK, TRACE, quote_ids=True, folder="mito")
    forms.pin_sniff(forms.read_source_text(rel), "clip")  # what the folder vote leaves behind
    monkeypatch.setattr(forms, "sniff_form", lambda text: "clip")
    seen: list = []
    fsm = _nucleate(monkeypatch, [rel], _ask(seen, ("A claim [Q1].", "Another [Q2].")))

    pins = fsm.run_state.file(0)
    assert (pins.form, pins.form_origin, pins.profile, pins.lane) == ("study", "stamp", "default", "outline")
    assert seen[0][0].endswith(ol.QUOTE_IDS_CLAIM)


def test_a_bare_marker_in_a_note_an_unstamped_source_fed_is_left_alone(tmp_vault):
    """A /web-search leaf banks quotes too: without the source's stamp CLEANUP
    never reads its bank, so a literal [Q1] in the note is the note's."""
    import types

    from silica.kernel.progress import RunManifestEntry
    from silica.router.states import finalize

    rel = _write("What do mitochondria do?", BODY, BANK, TRACE, quote_ids=False)
    note = "# Plans\n\nRevenue rose sharply in the first quarter [Q1] of the year.\n"
    tmp_vault.note("Concepts/Plans.md", note)
    entry = RunManifestEntry(title="Concepts/Plans", path="Concepts/Plans", parent=None, cluster_id=-1,
                             source_basename=Path(rel).name, op="patch")
    fsm = types.SimpleNamespace(manifest=types.SimpleNamespace(entries=[entry]), keep_sources=False,
                                seen_override=None, run_inverses=[])
    finalize._write_source_leaf(fsm, rel)

    out = _read("Concepts/Plans.md")
    assert "first quarter [Q1] of the year." in out
    assert "[[sources/What do mitochondria do]]" in out  # the leaf is still linked


# ------------------------------------------------------------- the leaf's place


def test_a_parts_leaf_sits_where_cleanup_looks_for_it(tmp_vault):
    """CLEANUP finds a leaf by the source's path under the inbox (ADR-0114).
    A part named by its basename alone was never found, so its notes got no
    Sources block and its quotes no leaf to resolve against."""
    from silica.kernel.base.manifest import source_key

    part = _write("Energy", BODY, BANK, TRACE, quote_ids=True, folder="mito")
    root = _write("Energy", BODY, BANK, TRACE, quote_ids=False)

    assert part == "Inbox/mito/Energy.md" and source_key(part) == "mito/Energy.md"
    assert "source_id: mito/Energy.md" in _read("sources/mito/Energy.md")
    assert root == "Inbox/Energy.md" and "source_id: Energy.md" in _read("sources/Energy.md")
    # A leaf left behind by an earlier part keeps the next part off its name.
    (Path(CONFIG.vault_path) / part).unlink()
    assert wr.unique_inbox_path("Energy", folder="mito") == "Inbox/mito/Energy 2.md"


def test_the_bank_reads_back_what_write_leaf_wrote(tmp_vault):
    bank = {"Q1": wr.Quote(url="https://a.example/x", quote="One line\nand its second line.", why="w"),
            "Q2": wr.Quote(url="https://b.example/y", quote="Short.", why="w2")}
    trace = ["Source: https://c.example\n[Q7] https://c.example/not-banked\n> a page line\nwhy: it says so"]
    wr.write_leaf("Inbox/x.md", trace, bank=bank)

    assert pv.evidence_bank(_read("sources/x.md")) == {
        "Q1": ("https://a.example/x", "One line and its second line."),
        "Q2": ("https://b.example/y", "Short."),
    }
    assert pv.evidence_bank("Source: https://c.example\nno bank here\n") == {}


# ------------------------------------------------------------ the instruction


def test_the_stages_that_write_a_line_are_told_to_keep_the_markers_only_for_a_part(monkeypatch):
    monkeypatch.setenv("SILICA_OUTLINE_BODIES", "llm")
    src = "## Energy\n\nCells run on ATP [Q1].\n\n## Genes\n\nDNA comes from the mother [Q2].\n"

    def run(quote_ids: bool) -> list[str]:
        seen: list[str] = []

        def ask(system, user, *, max_tokens, **kw):
            seen.append(system)
            if system.startswith(ol.STAGE_A_TAG):
                return {"lesson_title": "L", "spine": ["Cell energy"],
                        "ideas": [{"title": "Cell energy", "section": "Energy", "claim": "c [Q1]"}]}
            if system.startswith(ol.STAGE_GAP_TAG):
                return {"ideas": [], "skips": [{"section": "Genes", "reason": "r"}]}
            if system.startswith(ol.STAGE_BODIES_TAG):
                return {"bodies": {"Cell energy": "Cells run on ATP [Q1]."}}
            return {"edges": []}

        res = ol.run_outliner(source_text=src, source_basename="p.md", target="T", hub="H",
                              language="English", vault_outline=[], ask=ask, quote_ids=quote_ids)
        # A body the model writes reaches the op with its marker as written.
        assert "Cells run on ATP [Q1]." in next(o["snippet"] for o in res["updates"] if o["heading"] == "Cell energy")
        return seen

    plain, part = run(False), run(True)
    rels = ", ".join(ol.RELATIONS)
    assert plain[0] == ol.STAGE_A.format(lang="English", rels=rels)
    assert plain[1].startswith(ol.STAGE_GAP_TAG) and ol.QUOTE_IDS_CLAIM not in plain[1]
    assert plain[2] == ol.STAGE_BODIES.format(lang="English")
    assert part[0] == plain[0] + ol.QUOTE_IDS_CLAIM
    assert part[1] == plain[1] + ol.QUOTE_IDS_CLAIM
    assert part[2] == plain[2] + ol.QUOTE_IDS_BODY


# ---------------------------------------------------------- end to end, point 9


_FOOTNOTE_DEF = re.compile(r'^\[\^([^\]]+)\]: \[\[(.+?)\]\] "(.*)" <(\S+)>$', re.M)


def _claim_lines(note: str) -> list[str]:
    """The claim lines of an atomic note: every line of prose under the title
    up to its Sources or Relations, the `> claim` line included. Headings and
    blank lines are structure, not claims."""
    body = note.split("\n# ", 1)[1].split("\n", 1)[1]
    body = re.split(r"^## (?:Sources|Relations)\b", body, maxsplit=1, flags=re.M)[0]
    return [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]


def test_every_claim_line_of_a_nucleated_part_resolves_to_a_quote_and_its_url(tmp_vault, monkeypatch):
    """The test point 9 asks for: the spec's "ogni affermazione resta
    risalibile" made testable. A part whose every sentence cites its quote
    goes through /nucleate; every claim line of the atomic notes carries a
    footnote whose definition holds the quote and URL its leaf banked."""
    body = BODY.replace("passed down from the mother alone.\n", "passed down from the mother alone [Q2].\n")
    rel = _write("What do mitochondria do?", body, BANK, TRACE, quote_ids=True, folder="mito")
    seen: list = []
    fsm = _nucleate(monkeypatch, [rel], _ask(seen, ("Mitochondria make the ATP a cell spends [Q1].",
                                                    "Mitochondrial DNA comes from the mother [Q2, Q7].")))

    assert seen[0][0].endswith(ol.QUOTE_IDS_CLAIM)
    leaf = pv.evidence_bank(_read("sources/mito/What do mitochondria do.md"))
    assert set(leaf) == {"Q1", "Q2", "Q3"}
    key = "mito-What-do-mitochondria-do"
    for title in ("Cell energy", "Maternal inheritance"):
        note = _read(f"Research/{title}.md")
        defs = {m[0]: m[1:] for m in _FOOTNOTE_DEF.findall(note)}
        lines = _claim_lines(note)
        assert lines and lines[0].startswith("> ")
        for line in lines:
            assert not re.search(r"\[Q\d", line), line           # no bare marker survives
            refs = re.findall(rf"\[\^({key}-(Q\d+))\]", line)
            assert refs, f"claim line without its quote: {line!r}"
            for ref, qid in refs:
                url, quote = leaf[qid]
                assert defs[ref] == ("sources/mito/What do mitochondria do", quote, url)
        # Learn finds each claim's leaf off the definition, as for every leaf footnote.
        from silica.kernel.learn.items import claims_of

        assert {c["leaf"] for c in claims_of(f"Research/{title}.md", note)} == {
            "sources/mito/What do mitochondria do.md"}
    # The claim's phantom Q7 went, and the run says so.
    maternal = _read("Research/Maternal inheritance.md")
    assert "> Mitochondrial DNA comes from the mother[^mito-What-do-mitochondria-do-Q2]." in maternal
    # The quotes' definitions link the leaf: no bare link beside them.
    assert "\n[[sources/mito/What do mitochondria do]]\n" not in _read("Research/Cell energy.md")
    # A thin note of a part is not handed to the expand worker, which would
    # re-author it after CLEANUP and write over the keyed markers.
    assert "expand" not in {item.kind for item in fsm.queued}
    # One phantom, copied with its claim to three lines: the note's, the
    # spine's row and the spine's index of the file.
    assert fsm.run_state.files_summary[-1]["phantom_quotes"] == 3
    assert set(fsm.warning_ledger.paths("phantom_quote")) == {
        "Research/Maternal inheritance.md", "Research/What do mitochondria do.md"}


def test_two_parts_keep_their_own_q1_on_the_note_both_feed(tmp_vault, monkeypatch):
    """Each part has a Q1 of its own and both land in one note: the second
    part's WRITE runs after the first part's CLEANUP keyed its markers, and
    each key names its leaf."""
    def part(name: str, quote: str, url: str, sentence: str) -> str:
        bank = {"Q1": wr.Quote(url=url, quote=quote, why="w")}
        return _write(name, f"## Energy\n\n{sentence} [Q1].\n", bank, [f"Source: {url}\n{quote}"],
                      quote_ids=True, folder="mito")

    one = part("Part one", "ATP is the energy currency of the cell.", "https://one.example/a", "ATP carries the energy")
    two = part("Part two", "Glucose is broken down to make ATP.", "https://two.example/b", "Glucose feeds ATP")

    def ask(system, user, *, max_tokens, **kw):
        if system.startswith(ol.STAGE_A_TAG):
            return {"lesson_title": "Energy", "spine": ["Cell energy"],
                    "ideas": [{"title": "Cell energy", "section": "Energy", "claim": "Cells run on ATP [Q1].",
                               "depends_on": []}]}
        return {"edges": []}

    _nucleate(monkeypatch, [one, two], ask)

    note = _read("Research/Cell energy.md")
    assert "ATP carries the energy[^mito-Part-one-Q1]." in note
    assert "Glucose feeds ATP[^mito-Part-two-Q1]." in note
    defs = {m[0]: m[1:] for m in _FOOTNOTE_DEF.findall(note)}
    assert defs["mito-Part-one-Q1"] == ("sources/mito/Part one", "ATP is the energy currency of the cell.",
                                        "https://one.example/a")
    assert defs["mito-Part-two-Q1"] == ("sources/mito/Part two", "Glucose is broken down to make ATP.",
                                        "https://two.example/b")
    assert "[Q1]" not in note


def test_a_bare_marker_the_note_already_held_is_never_keyed_to_the_part(tmp_vault, monkeypatch):
    """The person's own `[Q1]` in a note a part patches is theirs: only the
    lines this chunk added are read as the part's."""
    tmp_vault.note("Research/Cell energy.md",
                   "---\ntype: Note\n---\n\n# Cell energy\n\nRevenue rose in the first quarter [Q1] of the year.\n")
    rel = _write("Energy", "## Energy\n\nATP carries the energy [Q1].\n",
                 {"Q1": wr.Quote(url="https://one.example/a", quote="ATP is the energy currency.", why="w")},
                 ["Source: https://one.example/a\nATP is the energy currency."], quote_ids=True, folder="mito")

    def ask(system, user, *, max_tokens, **kw):
        if system.startswith(ol.STAGE_A_TAG):
            return {"lesson_title": "Energy", "spine": ["Cell energy"],
                    "ideas": [{"title": "Cell energy", "section": "Energy", "claim": "Cells run on ATP [Q1].",
                               "depends_on": []}]}
        return {"edges": []}

    _nucleate(monkeypatch, [rel], ask)

    note = _read("Research/Cell energy.md")
    assert "Revenue rose in the first quarter [Q1] of the year." in note
    assert "ATP carries the energy[^mito-Energy-Q1]." in note


# ------------------------------------------------------------ the rewrite alone


_QBANK = {"Q1": ("https://a.example", "quote one"), "Q3": ("https://b.example", "quote three")}
_LINK = "[[sources/leaf]]"


def test_a_marker_naming_no_banked_quote_is_dropped_and_counted():
    note = "# T\n\nOne claim [Q1, Q9]. Another [Q8].\n"
    out, defs, dropped = pv.attribute_quotes(note, _QBANK, "leaf", _LINK)

    assert out == "# T\n\nOne claim[^leaf-Q1]. Another.\n"
    assert dropped == 2
    assert defs == ['[^leaf-Q1]: [[sources/leaf]] "quote one" <https://a.example>']


def test_two_leaves_q1_stay_apart_in_one_note():
    first, defs1, _ = pv.attribute_quotes("# T\n\nA claim [Q1].\n", _QBANK, "part-one", _LINK)
    first += "\n## Sources\n" + "\n".join(defs1) + "\nAnother claim [Q1].\n"
    # Only lines before Sources are claims; the second part adds its own line.
    second, defs2, _ = pv.attribute_quotes(
        first.replace("\n## Sources", "Another claim [Q1].\n\n## Sources", 1),
        {"Q1": ("https://z.example", "quote zed")}, "part-two", "[[sources/two]]")

    assert "A claim[^part-one-Q1]." in second and "Another claim[^part-two-Q1]." in second
    assert defs2 == ['[^part-two-Q1]: [[sources/two]] "quote zed" <https://z.example>']


def test_rerunning_the_rewrite_is_a_no_op():
    out, defs, _ = pv.attribute_quotes("# T\n\nA claim [Q3].\n", _QBANK, "leaf", _LINK)
    note = out + "\n## Sources\n" + "\n".join(defs) + "\n"

    assert pv.attribute_quotes(note, _QBANK, "leaf", _LINK) == (note, [], 0)


@pytest.mark.parametrize("line", [
    "## A heading [Q1]",
    "| cell [Q1] | other |",
    "```\ncode [Q1]\n```",
    "$$\nx [Q1]\n$$",
])
def test_the_lines_attribute_lines_keeps_out_of_keep_their_marker(line):
    note = f"---\nsummary: a [Q1] in the frontmatter\n---\n# T\n\n{line}\n\n## Sources\n[Q1] after the claims\n"
    assert pv.attribute_quotes(note, _QBANK, "leaf", _LINK) == (note, [], 0)


def test_a_link_named_like_a_quote_is_not_a_marker():
    note = "# T\n\nSee [[Q1]] and [Q3](https://x.example) for the claim [Q3].\n"
    out, _, _ = pv.attribute_quotes(note, _QBANK, "leaf", _LINK)
    assert out == "# T\n\nSee [[Q1]] and [Q3](https://x.example) for the claim[^leaf-Q3].\n"


def test_a_marker_opening_a_list_item_keeps_the_item():
    out, _, _ = pv.attribute_quotes("# T\n\n- [Q1] first point\n> [Q3] quoted\n", _QBANK, "leaf", _LINK)
    assert out == "# T\n\n- [^leaf-Q1] first point\n> [^leaf-Q3] quoted\n"


def test_the_preservation_rule_reads_the_rewrite_as_a_citation_not_a_loss():
    from silica.kernel.write.preserve import Loss, lost

    before = "# T\n\nA claim [Q1].\n- [Q3] point\nGone [Q9] here.\n"
    out, defs, _ = pv.attribute_quotes(before, _QBANK, "leaf", _LINK)
    after = out + "\n## Sources\n" + "\n".join(defs) + "\n"

    assert lost(before, after, loss=Loss.CITE) is None
    assert lost(before, after) is not None  # without the reason it would be one
