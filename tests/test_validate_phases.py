# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The phases validate_operations is being decomposed into, one test each.

The whole-batch oracle (tests/test_validate_characterization.py) says the
composition did not move; these say what each phase promises on its own.
"""
from __future__ import annotations

from silica.kernel.write.validate import _index_payloads


def test_index_payloads_keys_concepts_by_source_and_heading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # inbox_folders are absolute against the cwd
    payloads = [{"batches": [
        {"inbox_file": "Inbox/lesson.md", "concepts": [
            {"name": "Alpha", "inbox_excerpt": "Alpha text",
             "vault_collision": {"path": "Course/Alpha.md", "excerpt": "Alpha in the vault"}},
            {"name": "Beta", "inbox_excerpt": None, "vault_collision": None},
            {"name": "", "inbox_excerpt": "nameless, skipped"},
        ]},
        {"concepts": [{"name": "Orphan"}]},                       # no inbox_file: skipped
        {"inbox_file": "Other/notes.md", "concepts": []},        # a source with no concepts
    ]}]

    index = _index_payloads(payloads)

    assert index.valid_concepts == {"lesson.md": {"Alpha", "Beta"}, "notes.md": set()}
    assert index.source_files == {"lesson.md": "Inbox/lesson.md", "notes.md": "Other/notes.md"}
    assert index.inbox_folders == {str(tmp_path / "Inbox"), str(tmp_path / "Other")}
    assert index.concept_excerpts == {("lesson.md", "Alpha"): "Alpha text", ("lesson.md", "Beta"): ""}
    assert index.expected_collision_paths == {("lesson.md", "Alpha"): "Course/Alpha.md",
                                              ("lesson.md", "Beta"): None}
    assert index.collision_excerpts == {("lesson.md", "Alpha"): "Alpha in the vault"}


def test_index_payloads_of_nothing_is_empty():
    index = _index_payloads([])
    assert not index.valid_concepts and not index.inbox_folders and not index.source_files


def test_normalize_paths_repairs_in_order_and_counts_each_pattern():
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _normalize_paths

    def op(kind, path, **extra):
        return Op(op=kind, heading="H", source_basename="lesson.md", path=path, snippet="x", **extra)

    ops = [
        op(OpType.write, "Course/ML.md", title="Machine: Learning"),   # title wins over the heading path
        op(OpType.write, "Elsewhere/Out.md"),                           # a new note outside the boundary
        op(OpType.patch, "Elsewhere/Keep.md"),                          # an existing note: never rebased
        op(OpType.write, "docs/silica/Course/.secret.md"),              # a dot-file no walker would read
    ]
    counts: dict[str, int] = {}
    _normalize_paths(ops, target_dir="docs/silica/Course", write_root="docs/silica",
                     repaired=lambda k: counts.__setitem__(k, counts.get(k, 0) + 1))

    assert [o.path for o in ops] == [
        "docs/silica/Course/Machine Learning.md",
        "docs/silica/Elsewhere/Out.md",
        "Elsewhere/Keep.md",
        "docs/silica/Course/secret.md",
    ]
    assert counts == {"title_path": 1, "write_dir_rebase": 1, "path_slugify": 1}


def test_normalize_paths_without_a_boundary_only_slugifies():
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _normalize_paths

    ops = [Op(op=OpType.write, heading="H", source_basename="l.md", path="Course/Note: one.md", snippet="x")]
    _normalize_paths(ops, target_dir="Course", write_root="", repaired=lambda k: None)
    assert ops[0].path == "Course/Note one.md"


def test_dedup_paths_keeps_the_richest_op_per_note_and_skips_the_rest():
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _dedup_paths

    def op(kind, path, **body):
        return Op(op=kind, heading="H", source_basename="lesson.md", path=path, **body)

    ops = [
        op(OpType.patch, "Course/Note.md", snippet="short"),
        op(OpType.write, "course/note.md", snippet="the longer body wins"),   # same file, other spelling
        op(OpType.overwrite, "Course/Whole.md", content="a whole new body"),
        op(OpType.patch, "Course/Whole.md", snippet="x"),
        op(OpType.patch, "Course/Alone.md", snippet="untouched"),
    ]
    _dedup_paths(ops)

    assert [o.op for o in ops] == [OpType.skip, OpType.write, OpType.overwrite, OpType.skip, OpType.patch]
    assert ops[0].reason == "Duplicate operation to the same path 'Course/Note.md'"
    assert ops[2].content == "a whole new body"
    assert ops[4].reason is None


def test_dedup_paths_a_richer_patch_beside_an_overwrite_becomes_a_write():
    """Observed, not designed: the overwrite check runs after the losers are
    already skips, so only the winner's own type can still say overwrite."""
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _dedup_paths

    ops = [
        Op(op=OpType.patch, heading="H", source_basename="l.md", path="Course/N.md", snippet="richer"),
        Op(op=OpType.overwrite, heading="H", source_basename="l.md", path="Course/N.md", content="x"),
    ]
    _dedup_paths(ops)
    assert [o.op for o in ops] == [OpType.write, OpType.skip]
    assert ops[0].snippet == "richer"


def test_title_gate_reads_the_folder_once_and_answers_for_the_subtree_on_request(tmp_vault):
    from silica.kernel.text.title import title_key
    from silica.kernel.write.validate import TitleGate

    tmp_vault.note("Course/Regressione lineare.md", "# Regressione lineare\n")
    tmp_vault.note("Course/Same.md", "# Same\n")
    tmp_vault.note("Course/Sub/Same.md", "# Same\n")
    tmp_vault.note("Course/Sub/Deep.md", "# Deep\n")
    tmp_vault.note("Course/Perceptron.md", "---\naliases: [Percettrone]\n---\n# Perceptron\n")
    tmp_vault.note("Elsewhere/Regressione lineare.md", "# Regressione lineare\n")

    gate = TitleGate("Course")
    folder = gate.titles()
    deep = gate.titles(subtree=True)

    assert folder[title_key("Regressione lineare")][1].endswith("Course/Regressione lineare.md")
    assert title_key("Deep") not in folder and title_key("Deep") in deep
    assert deep[title_key("Same")][1].endswith("Course/Same.md")       # the folder entry wins
    assert folder[title_key("Percettrone")] == ("Percettrone", folder[title_key("Perceptron")][1])
    assert set(gate.names()) == {"Regressione lineare", "Same", "Perceptron", "Percettrone"}


def test_title_gate_abstains_on_a_folder_it_cannot_read(tmp_vault):
    from silica.kernel.write.validate import TitleGate
    assert TitleGate("Nowhere").titles() == {} and TitleGate("Nowhere").names() == []


def test_path_exists_memo_answers_once_per_path_and_per_call(tmp_vault):
    from silica.kernel.write.validate import _path_exists_memo

    tmp_vault.note("Course/Here.md", "# Here\n")
    path_exists = _path_exists_memo()
    assert path_exists("Course/Here.md") and not path_exists("Course/Later.md")
    tmp_vault.note("Course/Later.md", "# Later\n")
    assert not path_exists("Course/Later.md")          # the memo holds within a call
    assert _path_exists_memo()("Course/Later.md")       # a new call sees the vault


def test_coerce_types_decides_each_op_against_the_vault(tmp_vault):
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import (PayloadIndex, TitleGate, _coerce_types,
                                              _path_exists_memo)

    tmp_vault.note("Course/Existing.md", "# Existing\n")
    tmp_vault.note("Course/Regressione lineare.md", "# Regressione lineare\n")
    tmp_vault.note("Course/Lezione 11.md", "# Lezione 11\n")

    def op(kind, path, heading="H"):
        return Op(op=kind, heading=heading, source_basename="lesson.md", path=path, snippet="x")

    ops = [
        op(OpType.write, "Course/Existing.md"),                 # exists by path: patch
        op(OpType.write, "Course/Regressione Lineare.md"),      # exists by key-equal title: patch there
        op(OpType.write, "Course/Lezione 12.md"),               # numbers are identity: stays a write
        op(OpType.patch, "Course/Missing.md", heading="Free"),  # nothing to patch: write
        op(OpType.patch, "Course/Missing.md", heading="Bound"), # the payload named another target: stays
        Op(op=OpType.skip, heading="S", source_basename="lesson.md", reason="left alone"),
    ]
    index = PayloadIndex(expected_collision_paths={("lesson.md", "Bound"): "Course/Existing.md"})
    _coerce_types(ops, index=index, has_payloads=True, title_gate=TitleGate("Course"),
                  path_exists=_path_exists_memo(), hub="Course")

    assert [o.op for o in ops] == [OpType.patch, OpType.patch, OpType.write,
                                   OpType.write, OpType.patch, OpType.skip]
    assert ops[1].path.endswith("Course/Regressione lineare.md")
    assert [o.hub for o in ops] == ["Course"] * 5 + [None]


def test_dedup_coerced_paths_keeps_the_richest_op_per_retargeted_note():
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _dedup_coerced_paths

    ops = [
        Op(op=OpType.patch, heading="A", source_basename="l.md", path="Course/Same.md", snippet="short"),
        Op(op=OpType.patch, heading="B", source_basename="l.md", path="Course/Same.md", snippet="the longer one"),
        Op(op=OpType.skip, heading="C", source_basename="l.md", path="Course/Same.md", reason="already out"),
    ]
    _dedup_coerced_paths(ops)
    assert [o.op for o in ops] == [OpType.skip, OpType.patch, OpType.skip]
    assert ops[0].reason == "Duplicate operation to the same path 'Course/Same.md' after coercion"
    assert ops[2].reason == "already out"


def test_grounding_check_clears_a_span_found_elsewhere_in_the_source(tmp_vault, monkeypatch):
    monkeypatch.delenv("SILICA_EXTRACTIVE_ENFORCE", raising=False)
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import Grounding, PayloadIndex

    tmp_vault.note("Inbox/lesson.md", "Intro.\n\nThe closing law is $E_{tot} = m c^2 + p c$ here.\n")
    index = PayloadIndex(source_files={"lesson.md": "Inbox/lesson.md"},
                         concept_excerpts={("lesson.md", "Law"): "The window shows $a^2 + b^2 = c^2$ only."})
    ungrounded: list = []
    scores: list = []
    grounding = Grounding(index, profile="default", ungrounded_out=ungrounded, grounding_out=scores)

    def op(body):
        return Op(op=OpType.write, heading="Law", source_basename="lesson.md", path="Course/Law.md", snippet=body)

    grounding.check(op("Both $a^2 + b^2 = c^2$ and $E_{tot} = m c^2 + p c$ are stated."))
    assert ungrounded == []                       # verbatim outside the window is not fabrication
    assert scores[-1]["ungrounded"] == 0 and scores[-1]["profile"] == "default"

    grounding.check(op("Invented: $\\int_0^1 x^3 \\, dx = 1/4$ appears nowhere."))
    assert [row["path"] for row in ungrounded] == ["Course/Law.md"]
    assert scores[-1]["ungrounded"] == 1


def test_grounding_extractive_reject_fires_only_under_an_extractive_profile(monkeypatch):
    monkeypatch.delenv("SILICA_EXTRACTIVE_ENFORCE", raising=False)
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import Grounding, PayloadIndex

    index = PayloadIndex(concept_excerpts={("lesson.md", "H"): "The source says exactly this line.\nAnd this other line."})

    def op(body):
        return Op(op=OpType.write, heading="H", source_basename="lesson.md", path="Course/H.md", snippet=body)

    def judge(profile, body):
        return Grounding(index, profile=profile, ungrounded_out=None, grounding_out=None).extractive_reject(op(body))

    assert judge("default", "A paraphrase the source never says.") is None
    assert judge("extractive", "The source says exactly this line.") is None
    reason = judge("extractive", "A paraphrase the source never says.")
    assert reason and reason.startswith("extractive: 1 body line(s) not verbatim from source")


def test_grounding_reads_the_whole_source_from_the_inbox_or_reports_nothing(tmp_vault):
    from silica.kernel.write.validate import Grounding, PayloadIndex

    tmp_vault.note("Inbox/lesson.md", "the whole lecture\n")
    grounding = Grounding(PayloadIndex(source_files={"lesson.md": "Inbox/lesson.md"}),
                          profile="default", ungrounded_out=None, grounding_out=None)
    assert "the whole lecture" in grounding.full_source_text("lesson.md")
    assert grounding.full_source_text("never-seen.md") == ""


def test_record_dangling_links_names_only_what_resolves_nowhere(tmp_vault):
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _path_exists_memo, _record_dangling_links

    tmp_vault.note("Course/Known.md", "# Known\n")
    body = "See [[Known]], [[Sibling]], [[Promised]], [[Course/Known]] and [[Nowhere]]."
    ops = [
        Op(op=OpType.write, heading="Main", source_basename="l.md", path="Course/Main.md", snippet=body),
        Op(op=OpType.write, heading="Sibling", source_basename="l.md", path="Course/Sibling.md", snippet="x"),
        Op(op=OpType.move, heading="M", source_basename="l.md", from_path="Course/A.md", to_path="Course/B.md"),
    ]
    cleared: list = []
    _record_dangling_links(ops, future_ref_whitelist=["Promised"], path_exists=_path_exists_memo(),
                           cleared_links_out=cleared)

    assert cleared == [{"cleared_link": "Nowhere", "note_heading": "Main", "note_path": "Course/Main.md"}]
    assert [o.op for o in ops] == [OpType.write, OpType.write, OpType.move]   # nothing is dropped

    _record_dangling_links(ops, future_ref_whitelist=None, path_exists=_path_exists_memo(),
                           cleared_links_out=None)                              # no sink, no error


def test_missing_hub_ops_writes_each_absent_hub_once_in_the_target_folder(tmp_vault):
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _missing_hub_ops, _path_exists_memo

    tmp_vault.note("Course/Known.md", "# Known\n")

    def op(kind, path, hub, heading="H", source_basename="lesson.md"):
        return Op(op=kind, heading=heading, source_basename=source_basename, path=path,
                  snippet="x", hub=hub)

    ops = [
        op(OpType.write, "Course/A.md", "Course"),                       # the folder is not a note
        op(OpType.patch, "Course/B.md", "[[Other]]"),                    # bracketed spelling, same hub
        op(OpType.write, "Course/C.md", "Course"),                       # named twice, created once
        op(OpType.write, "Course/Third.md", "Third", heading="Third"),   # the batch writes it by heading
        op(OpType.write, "Course/Fourth.md", "Fourth"),                  # ...and this one by path
        op(OpType.overwrite, "Course/Known.md", "Known"),                # exists in the vault
        Op(op=OpType.move, heading="M", source_basename="l.md", from_path="Course/A.md", to_path="Course/Z.md", hub="Moved"),
    ]
    hub_ops = _missing_hub_ops(ops, target_dir="Course", path_exists=_path_exists_memo())

    assert [(o.op, o.heading, o.path, o.hub) for o in hub_ops] == [
        (OpType.write, "Course", "Course/Course.md", "Course"),
        (OpType.write, "Other", "Course/Other.md", "Other"),
    ]
    assert {o.source_basename for o in hub_ops} == {"lesson.md"}
    assert hub_ops[0].snippet == "Hub automatically generated by the Injector pipeline."
    assert _missing_hub_ops([], target_dir="Course", path_exists=_path_exists_memo()) == []
    # a merge's alias rewrites an owner's note with the folder's name as its hub:
    # no hub is made for it (an empty "Teoria della probabilità", 2026-10-23)
    tmp_vault.note("Prob/Variabile casuale.md", "# Variabile casuale\n")
    alias = op(OpType.overwrite, "Prob/Variabile casuale.md", "Prob")
    assert _missing_hub_ops([alias], target_dir="Prob", path_exists=_path_exists_memo()) == []


def _batch(tmp_vault, **overrides):
    from silica.kernel.write.validate import (Grounding, PayloadIndex, TitleGate, _Batch,
                                              _path_exists_memo)
    fields = dict(
        target_dir="Course", target_dir_abs="", write_root="", mirror=False, vault_root="",
        has_payloads=False, index=PayloadIndex(), title_gate=TitleGate("Course"),
        path_exists=_path_exists_memo(),
        grounding=Grounding(PayloadIndex(), profile="default", ungrounded_out=None, grounding_out=None),
        justified_folders=set(), run_write_stems=set(), normalized_out=None, cleared_parents_out=None,
    )
    return _Batch(**{**fields, **overrides})


def test_batch_resolve_parent_keeps_the_run_and_the_vault_and_clears_the_rest(tmp_vault):
    from silica.kernel.write.ops import Op, OpType

    tmp_vault.note("Course/Known.md", "# Known\n")
    cleared: list = []
    batch = _batch(tmp_vault, run_write_stems={"sibling"}, cleared_parents_out=cleared)

    def op(parent):
        return Op(op=OpType.write, heading="H", source_basename="l.md", path="Course/N.md",
                  snippet="x", parent=parent)

    kept = [op("Sibling"), op("Known"), op(None)]
    ghost = op("Ghost")
    for o in kept + [ghost]:
        batch.resolve_parent(o)

    assert [o.parent for o in kept] == ["Sibling", "Known", None]
    assert ghost.parent is None
    assert cleared == [{"cleared_parent": "Ghost", "note_heading": "H", "note_path": "Course/N.md"}]


def test_batch_repaired_counts_into_the_caller_sink_only_when_there_is_one(tmp_vault):
    counts: dict = {}
    batch = _batch(tmp_vault, normalized_out=counts)
    batch.repaired("title_path"); batch.repaired("title_path"); batch.repaired("path_slugify")
    assert counts == {"title_path": 2, "path_slugify": 1}
    _batch(tmp_vault).repaired("title_path")            # no sink: nothing to count into


def test_justified_folders_names_the_landing_folder_and_every_reasoned_new_one(tmp_path):
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _invented_folder, _justified_folders

    (tmp_path / "Existing").mkdir()
    (tmp_path / "silica" / "Mirrored").mkdir(parents=True)
    root = str(tmp_path)

    assert _invented_folder("silica/Existing/A.md", root) == ""      # the vault has it
    assert _invented_folder("silica/Mirrored/A.md", root) == ""      # an earlier run made it
    assert _invented_folder("silica/Fresh/Deep/A.md", root) == "Fresh/Deep"
    assert _invented_folder("silica/A.md", root) == ""               # the mirror root itself

    def write(path, reason=None):
        return Op(op=OpType.write, heading="H", source_basename="l.md", path=path, snippet="x", reason=reason)

    ops = [write("silica/Fresh/A.md", reason="no existing folder fits"), write("silica/Quiet/B.md")]
    assert _justified_folders(ops, mirror=True, target_dir="silica/Course", vault_root=root) == {"course", "fresh"}
    assert _justified_folders(ops, mirror=False, target_dir="silica/Course", vault_root=root) == set()


def test_validate_patch_judges_the_real_note_and_lands_on_its_mirror(tmp_vault):
    import os
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import PayloadIndex, _validate_patch

    tmp_vault.note("Course/Existing.md", "# Existing\n")
    tmp_vault.note("Elsewhere/Existing.md", "# Elsewhere\n")

    def patch(path, heading="H"):
        return Op(op=OpType.patch, heading=heading, source_basename="lesson.md", path=path, snippet="x")

    plain = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"))
    assert _validate_patch(patch("Course/Existing.md"), plain) is None
    assert "does not exist in vault" in _validate_patch(patch("Course/Missing.md"), plain).reason
    assert _validate_patch(patch("Elsewhere/Existing.md"), plain).reason == "Path 'Elsewhere/Existing.md' not in target folder"

    bound = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"), has_payloads=True,
                   index=PayloadIndex(expected_collision_paths={("lesson.md", "H"): "Course/Existing.md"}))
    assert "does not match expected collision" in _validate_patch(patch("Course/Other.md"), bound).reason
    assert _validate_patch(patch("Course/Existing.md"), bound) is None

    counts: dict = {}
    mirrored = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"), has_payloads=True,
                      mirror=True, write_root="silica", normalized_out=counts,
                      index=PayloadIndex(expected_collision_paths={("lesson.md", "H"): "Course/Existing.md"}))
    op = patch("silica/Course/Existing.md")            # the model read the target in mirror space
    assert _validate_patch(op, mirrored) is None
    assert op.path == "silica/Course/Existing.md"      # judged unmirrored, landed on the mirror copy
    assert counts == {"mirror_patch_unrebase": 1, "mirror_patch_rebase": 1}


def test_validate_write_lands_flags_or_rejects_by_body_and_title(tmp_vault, monkeypatch):
    import os
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import PayloadIndex, _Skip, _validate_write

    for name in ("SILICA_MIN_WRITE_SNIPPET_CHARS", "SILICA_META_SHAPE_CHECK", "SILICA_EXTRACTIVE_ENFORCE"):
        monkeypatch.delenv(name, raising=False)
    tmp_vault.note("Course/Description.md", "# Description\n")
    rich = ("Gradient descent updates the weights against the gradient of the loss, scaled by "
            "the learning rate; too large a rate diverges, too small a rate crawls. ") * 2
    announcement = ("This section provides an overview of the topic and includes the formal "
                    "definition and a practical example of its use, ") * 3

    def write(path, body=rich, heading="H"):
        return Op(op=OpType.write, heading=heading, source_basename="lesson.md", path=path, snippet=body)

    batch = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"))
    assert _validate_write(write("Elsewhere/New.md"), batch).reason == "Path 'Elsewhere/New.md' not in target folder"
    assert "already exists" in _validate_write(write("Course/Description.md"), batch).reason
    assert "meta-description" in _validate_write(write("Course/New.md", announcement), batch).reason
    assert "snippet too short (0 <" in _validate_write(write("Course/New.md", ""), batch).reason

    near = write("Course/Descriptor.md")
    assert _validate_write(near, batch) is None
    assert "near_title candidate='Description'" in (near.review or "")

    thin = write("Course/Thin.md", "A true but short fact.")
    assert _validate_write(thin, batch) is None
    assert "snippet too short" in (thin.review or "")

    bound = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"), has_payloads=True,
                   index=PayloadIndex(concept_excerpts={("lesson.md", "Mention"): "", ("lesson.md", "Defined"): "text"}))
    assert _validate_write(write("Course/M.md", "", heading="Mention"), bound) is _Skip.FORWARD_REFERENCE
    assert "snippet too short (0 <" in _validate_write(write("Course/D.md", "", heading="Defined"), bound).reason


def test_validate_overwrite_degrades_to_write_or_snapshots_the_base(tmp_vault):
    import os
    from silica.driver import DRIVER
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import _validate_overwrite

    tmp_vault.note("Course/Existing.md", "# Existing\n\nthe old body\n")
    batch = _batch(tmp_vault, target_dir_abs=os.path.abspath("Course"))

    def overwrite(path, **extra):
        return Op(op=OpType.overwrite, heading="H", source_basename="l.md", path=path, content="new body", **extra)

    outside = overwrite("Elsewhere/X.md")
    assert _validate_overwrite(outside, batch).reason == "Path 'Elsewhere/X.md' not in target folder"

    forgot = overwrite("Course/Existing.md")
    assert _validate_overwrite(forgot, batch) is None
    assert forgot.op is OpType.overwrite
    assert forgot.base_content == DRIVER.read_note("Course/Existing.md").content

    remembered = overwrite("Course/Existing.md", base_content="what the producer read")
    assert _validate_overwrite(remembered, batch) is None
    assert remembered.base_content == "what the producer read"

    missing = overwrite("Course/Missing.md")
    assert _validate_overwrite(missing, batch) is None
    assert (missing.op, missing.snippet) == (OpType.write, "new body")


def test_admit_guards_the_boundary_the_invented_folder_and_the_inbox(tmp_vault):
    import os
    from silica.config import CONFIG
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import PayloadIndex, _admit

    def op(kind, path, heading="H", source_basename="lesson.md"):
        return Op(op=kind, heading=heading, source_basename=source_basename, path=path, snippet="x")

    bounded = _batch(tmp_vault, write_root="docs")
    assert "outside the vault write boundary 'docs/'" in _admit(op(OpType.write, "Elsewhere/A.md"), bounded).reason
    assert _admit(op(OpType.write, "docs/A.md"), bounded) is None

    mirrored = _batch(tmp_vault, mirror=True, write_root="silica", vault_root=CONFIG.vault_path,
                      justified_folders={"fresh"})
    assert _admit(op(OpType.patch, "Course/Real.md"), mirrored) is None          # exempt from the boundary
    assert _admit(op(OpType.write, "silica/Fresh/A.md"), mirrored) is None       # the batch justified it
    assert "'Quiet/' does not exist" in _admit(op(OpType.write, "silica/Quiet/A.md"), mirrored).reason

    plain = _batch(tmp_vault)
    assert "forbidden inbox segment" in _admit(op(OpType.patch, "Course/0 Inbox/A.md"), plain).reason
    inboxed = _batch(tmp_vault, index=PayloadIndex(inbox_folders={os.path.abspath("Inbox")}))
    assert "forbidden inbox segment" in _admit(op(OpType.patch, "Inbox/Sub/A.md"), inboxed).reason


def test_admit_corrects_heading_and_source_against_the_payload(tmp_vault, monkeypatch):
    from silica.kernel.write.ops import Op, OpType
    from silica.kernel.write.validate import PayloadIndex, _admit

    monkeypatch.setattr("silica.config.CONFIG.cooccurrence_lang", "auto")

    def op(heading, source_basename="lesson.md"):
        return Op(op=OpType.write, heading=heading, source_basename=source_basename, path="Course/A.md", snippet="x")

    one_source = _batch(tmp_vault, has_payloads=True, index=PayloadIndex(
        valid_concepts={"lesson.md": {"L'algoritmo", "dataframe spark", "Stima dei parametri"}}))
    assert _admit(op(""), one_source).reason == "Missing 'heading' field"
    assert _admit(op("H", source_basename=""), one_source).reason == "Missing 'source_basename' field"

    coerced = op("L’ALGORITMO", source_basename="typo.md")
    assert _admit(coerced, one_source) is None
    assert (coerced.heading, coerced.source_basename) == ("L'algoritmo", "lesson.md")   # normalized, lone source
    subset = op("DataFrame")
    assert _admit(subset, one_source) is None and subset.heading == "dataframe spark"
    stemmed = op("Stima parametrica")
    assert "not present" in _admit(stemmed, one_source).reason        # "auto" stems nothing
    monkeypatch.setattr("silica.config.CONFIG.cooccurrence_lang", "italian")
    assert _admit(stemmed, one_source) is None and stemmed.heading == "Stima dei parametri"
    assert "not present in payload concepts" in _admit(op("Nowhere near"), one_source).reason

    two_sources = _batch(tmp_vault, has_payloads=True, index=PayloadIndex(
        valid_concepts={"lesson.md": {"A"}, "other.md": {"B"}}))
    assert _admit(op("A", source_basename="typo.md"), two_sources).reason == "Unknown source_basename 'typo.md'"
