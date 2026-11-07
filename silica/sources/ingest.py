# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Ingest as a service: what happens to a source between a person naming it and
the FSM running on it.

Triage (which lane a file takes), batch resume, draft filing, the folder picks,
the drain of the capture WAL and the promotion out of episodic memory. No surface
lives here: the caller hands in a `console` to print to, the way `mail_cli.run`
and `connect_cli.run` take one, so the same run reads the same on a terminal and
inside the web turn's capture.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from silica.config import CONFIG

logger = logging.getLogger(__name__)


def _announced_target(target_dir: str) -> str:
    """The folder the user will find the notes in, write-boundary included.

    Validate rebases every write into the vault's `write_dir`, so announcing
    the raw target ("nucleate: 2 file(s) → appunti") named a folder nothing
    lands in — the user opened it, found nothing, and concluded the run wrote
    nothing. Display-only: the Coordinator still takes the raw target.
    """
    try:
        from silica.kernel.base.manifest import in_write_dir

        return in_write_dir(target_dir)
    except Exception:
        return target_dir


def _draft_title(body: str, stem: str) -> str:
    """First `# ` heading, else the file stem. Never a prose fragment: the
    audit's `minutes of compute` came from letting a model pick a mid-sentence
    span, so this stays mechanical."""
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            # Obsidian refuses * " \ / < > : | ? in a note name; a study guide
            # titled "Machine Learning: guida allo studio" landed as a file
            # the app would not rename or link cleanly.
            title = re.sub(r"\s*:\s*", " - ", line[2:].strip()).replace("/", "-")
            return re.sub(r'[*"\\<>|?]', "", title).strip() or stem
    return stem


def file_drafts(
    md_files: list[str], target_dir: str, undo_run: str | None, console, ask=None
) -> list[str]:
    """File draft-form sources instead of distilling them; return the rest.

    docs/specs/nucleation-forms.md: a draft is the owner's in-progress
    artifact — both distillation outcomes observed on drafts were defects
    (silent drop, verbatim paste under a fragment title). Filing = one note at
    the resolved target, body intact, `form: draft` frontmatter, /revert
    coverage, source archived. A surface with a person at it hands in `ask`,
    which takes the question and returns the answer; without one the run files
    and reports. A "n" answer sends the file down normal nucleation under the
    vault fallback profile.
    """
    import silica.kernel.forms as forms
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter
    from silica.kernel.write import gate

    kept: list[str] = []

    # Resolve every file's form upfront on a small pool: an unstamped file
    # costs one sniff LLM call and the calls are independent — 14 lecture
    # files paid ~15s serially for verdicts this loop then consumes in order.
    # The interactive draft prompt below stays sequential.
    def _read_and_resolve(f: str):
        try:
            text = forms.read_source_text(f)
            return text, forms.resolve(text)
        except Exception:
            return None

    if len(md_files) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(md_files))) as pool:
            resolved = list(pool.map(_read_and_resolve, md_files))
    else:
        resolved = [_read_and_resolve(f) for f in md_files]

    # Siblings of one folder are one kind of material, and the sniff is not
    # stable on them: the same 14 lectures came back 6 study / 4 transcript /
    # 4 unsure in one run and one of them "draft" in the next, which filed a
    # lecture as-is instead of distilling it (2026-09-05). The folder votes
    # among the sniffed verdicts; sniffed and unsure files follow the vote,
    # stamps and a configured vault profile do not. A tie keeps each verdict.
    if len(md_files) > 1:
        from collections import Counter
        votes = Counter(rr[1].form for rr in resolved if rr and rr[1].origin == "sniff" and rr[1].form)
        top = votes.most_common(2)
        if top and (len(top) == 1 or top[0][1] > top[1][1]):
            folder_form = top[0][0]
            voted = forms.Form(folder_form, forms.profile_for(folder_form), "folder vote")
            for i, rr in enumerate(resolved):
                if rr and rr[1].origin in ("sniff", "default") and rr[1] != voted:
                    forms.pin_sniff(rr[0], folder_form)
                    resolved[i] = (rr[0], voted)
            console.print(
                f"  folder vote: [bold]{folder_form}[/] "
                f"({votes[folder_form]} of {sum(votes.values())} sniffed file(s))"
            )

    for f, rr in zip(md_files, resolved):
        if rr is None:
            kept.append(f)
            continue
        text, res = rr
        if res.form != "draft":
            # Visibility is non-negotiable (nucleation-forms spec): the lens
            # verdict prints where auto-target is announced, never silently.
            console.print(
                f"  {f}: profile [bold]{res.profile or 'default'}[/] ({res.origin})"
            )
            kept.append(f)
            continue
        if ask is not None:
            ans = ask(
                f"  {f} looks like a draft of yours ({res.origin}) — file it "
                f"as-is instead of distilling? [Y/n] "
            ).strip().lower()
            if ans in ("n", "no"):
                kept.append(f)
                continue
        try:
            dest_dir = target_dir or pick_target_folder([f])
        except Exception:
            console.print(f"  [yellow]{f}: draft, but no target resolved — distilling instead[/]")
            kept.append(f)
            continue
        # The filed copy is a Silica write like any other: composed into the
        # write boundary. A bare-folder pick used to land the draft in the
        # user's source tree, beside their own files.
        from silica.kernel.base.manifest import in_write_dir
        dest_dir = in_write_dir(dest_dir)
        data, _, body = frontmatter.split(text)
        data = dict(data or {})
        data["form"] = "draft"
        data.setdefault("source_file", Path(f).name)
        note = frontmatter.dump(data, f"{body.strip()}\n")
        lab = str(data.get("source_file", "")).lower().endswith(".ipynb")
        if lab:
            from silica.kernel.link import labs
            note += labs.practised_section(body, labs.folder_titles(dest_dir))
        # A notebook's first heading names its first cell ("Dataset uploading",
        # two optdigits labs of one course), not the lab: its file name does.
        title = Path(f).stem if lab else _draft_title(body, Path(f).stem)
        note_rel = f"{dest_dir}/{title}.md"
        prior: str | None = None
        try:
            prior = DRIVER.read_note(note_rel).content
        except Exception:
            prior = None  # absent; an unreadable note fails the upsert below, which reports it
        if prior is not None:
            note_rel = f"{dest_dir}/{title} ({Path(f).stem}).md"
            prior = None
        DRIVER.upsert(note_rel, note)
        if undo_run:
            gate.record_landed(undo_run, gate.undo_of(note_rel, prior))
        _autolink_filed(note_rel, dest_dir, console)
        gate.archive_source(f)
        console.print(
            f"  filed draft ({res.origin}): [bold]{note_rel}[/] — body kept intact"
        )
    return kept


def _autolink_filed(note_rel: str, folder: str, console) -> None:
    """Link a filed note to the notes of its folder it names: the filing path
    of docs/specs/nucleation-forms.md says autolink still runs so the note
    joins the graph, and it never did. A study guide or a lecture digest
    names the concepts the folder holds; the folder, not the whole vault,
    keeps "Statistica" in a psychology folder from catching it. The filed
    note is new, so the run's /revert, which deletes it, covers this edit."""
    from silica.driver import DRIVER
    from silica.kernel.link.autolink import build_title_index

    try:
        added = DRIVER.autolink_note(note_rel, title_index=build_title_index(DRIVER.list_files(folder)))
    except Exception as e:
        # Best effort, as the FSM's own AUTOLINK phase: the note is filed whole
        # either way, it only stays unlinked.
        logger.debug("filed note %s not autolinked: %s", note_rel, e)
        return
    if added:
        console.print(f"  [dim]{note_rel}: linked {len(added)} note(s) it names[/]")


def prior_conversions() -> dict[str, dict]:
    """abspath of a converted source → {"inbox": [segment paths], "done": n}.

    The two places a source's converted segments can already live: still in the
    inbox (a run that never finished) and archived in done/ (a run that did).
    Read once per /nucleate invocation off the `source_file` frontmatter that
    convert() stamps on every segment — the identity `resolve_input` produced
    at conversion time, so the same resolver matches it on re-run.
    """
    from silica.driver import DRIVER
    from silica.kernel.base.manifest import active_done_dir, active_inbox_dir
    from silica.kernel.base import frontmatter

    out: dict[str, dict] = {}
    vault = Path(CONFIG.vault_path or "")

    def _scan(rels: list[str], bucket: str) -> None:
        for rel in rels:
            try:
                data, _, _ = frontmatter.split(
                    (vault / rel).read_text(encoding="utf-8"))
            except Exception:
                continue
            src = str((data or {}).get("source_file") or "")
            if not src:
                continue
            if "/" in src and not Path(src).is_absolute():
                # The server's vault path (ADR-0129 point 1), keyed as
                # `resolve_input` spells the same file; a bare name is an
                # older server note whose file is gone, and matches nothing.
                src = str(vault / src)
            entry = out.setdefault(src, {"inbox": [], "done": 0})
            if bucket == "inbox":
                entry["inbox"].append(rel)
            else:
                entry["done"] += 1

    if active_inbox_dir():
        _scan([r.path for r in DRIVER.list_inbox_files()
               if r.path.endswith(".md")], "inbox")
    done = active_done_dir()
    if done:
        _scan([r.path for r in DRIVER.list_files(done)], "done")
    from silica.kernel.recall.listing import natural_key
    for entry in out.values():
        entry["inbox"].sort(key=natural_key)  # segment order is ingest order
    return out


def _shallow_folders(vault_path: str, max_depth: int = 2) -> set[str]:
    """Vault-relative folders holding at least one file, down to `max_depth`.

    ponytail: depth-capped instead of a full walk — a scanned-book library has
    one folder per photographed artefact and the deep tail is noise, not a
    destination. Raise the cap if someone files notes deeper than two levels
    in a folder that holds no markdown yet.
    """
    root = Path(vault_path or "")
    if not root.is_dir():
        return set()
    out: set[str] = set()
    for depth in range(1, max_depth + 1):
        for d in root.glob("/".join(["*"] * depth)):
            if not d.is_dir():
                continue
            rel = d.relative_to(root).as_posix()
            if any(p.startswith(".") for p in d.relative_to(root).parts):
                continue
            if any(c.is_file() and not c.name.startswith(".") for c in d.iterdir()):
                out.add(rel)
    return out


def pick_target_folder(md_files: list[str]) -> str:
    """Choose the destination folder for a nucleate run with ONE small LLM call.

    Replaces a full agent turn: the old auto-target path resent the entire
    session history to the orchestrator to make this single decision.
    Raises on any failure — the caller falls back to the agent message.
    """
    from silica.model.llm import call_llm

    from silica.kernel.base.manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    listed = _listed_folders(inbox)
    # read_source_text, not DRIVER.read_note: a .txt inbox source (draft
    # filing's ep180 case) must be auto-targetable too.
    from silica.kernel.forms import read_source_text

    excerpt = read_source_text(md_files[0])[:1500]
    prompt = (
        "Pick the single most relevant destination folder for nucleating this "
        "content into a knowledge vault. Reply with ONLY the folder path on one "
        "line, no quotes. Prefer an existing folder; invent a sensible new path "
        "only if nothing fits.\n\n"
        "Existing folders:\n" + _folders_listing(listed)
        # The file's name, not its inbox path: shown `Inbox/machine_learning/
        # Lezione 1.md`, a Qwen3.5-9B answered `machine_learning` (2026-10-12).
        + f"\n\nContent excerpt ({Path(md_files[0]).name}):\n{excerpt}"
    )
    # reasoning=False: the answer is one line, and a hybrid model left to think
    # spent all 2048 tokens on its trace and answered "" (Qwen3.5-9B on
    # llama-server, 2026-10-12), which dropped the batch to a full agent turn.
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=2048,
                    reasoning=False)
    lines = [ln.strip().strip('"').strip("`").rstrip("/") for ln in (resp.text or "").splitlines()]
    pick = next((ln for ln in lines if ln), "")
    if not pick:
        raise ValueError("empty folder pick")
    return _resolve_pick(pick, listed, inbox)


def _listed_folders(inbox: str) -> list[str]:
    """The folders a pick is shown as existing: never the inbox, a dot-folder
    or a census-skipped one."""
    from silica.driver import DRIVER
    from silica.kernel.base.layout import NOT_A_DESTINATION

    # Note parents (any depth) PLUS the shallow tree of folders that hold
    # anything at all. A research library is folders of PDFs: censusing note
    # parents alone showed the model an EMPTY list, whereupon it echoed back the
    # inbox subfolder the source sat in and VALIDATE rejected all 31 ops.
    folders = {
        str(Path(r.path or r.name).parent)
        for r in DRIVER.list_files("")
    } | _shallow_folders(CONFIG.vault_path)
    return sorted(
        f for f in folders - {".", inbox}
        if not f.startswith(f"{inbox}/")
        and not any(part.startswith(".") or part.casefold() in NOT_A_DESTINATION
                    for part in Path(f).parts)
    )


def _folders_listing(listed: list[str]) -> str:
    if listed:
        return "\n".join(f"- {f}" for f in listed[:200])
    # Say it, never leave the list blank: on a fresh vault the model spent its
    # whole reply on "nothing follows 'Existing folders:'" before guessing (473
    # completion tokens for two words, 2026-08-23).
    return ("(no folders yet: this vault holds only its inbox, so name a "
            "sensible new top-level folder)")


def _resolve_pick(pick: str, listed: list[str], inbox: str) -> str:
    """A model's folder answer as a vault folder, or ValueError."""
    # Same rule VALIDATE applies to every op, asked once here instead of once
    # per rejected op an hour of LLM calls later.
    from silica.kernel.base.layout import contain_in_vault, is_inbox_path

    # The excerpt above is INGESTED DOCUMENT text, so this reply is an
    # injection channel and the string it carries becomes a write destination
    # (`<dest>/<title>.md` → DRIVER.upsert). Contain it here, where the caller
    # still has a fallback, rather than letting an absolute or `..`-bearing
    # pick reach the driver: `in_write_dir` only prefixes, it never normalises.
    norm = contain_in_vault(pick.replace("\\", "/"), Path(CONFIG.vault_path))
    # The same folder spelt another way lands in the one the vault has: three
    # runs of the same 14 lectures answered `machine_learning`, `Inbox/
    # machine_learning` and `Machine learning` for the existing `Machine
    # learning` (2026-10-12), and a new sibling splits the notes from their hub.
    # A full-path match first, then a unique folder of the same name anywhere.
    # A pick inside the inbox is matched on what follows the inbox and kept
    # only when it matches: otherwise it is the source's own folder echoed
    # back, and the agent fallback chooses instead of a new inbox-named folder.
    parts = Path(norm).parts
    in_inbox = bool(parts) and parts[0] == inbox
    if in_inbox:
        norm = "/".join(parts[1:])
    if norm and norm not in listed:
        def fold(s: str) -> str:
            return re.sub(r"[\s_\-]+", " ", s).strip().casefold()
        same = [f for f in listed if fold(f) == fold(norm)]
        if not same and "/" not in norm:
            same = [f for f in listed if fold(Path(f).name) == fold(norm)]
        if len(same) == 1:
            norm = same[0]
    if not norm or (in_inbox and norm not in listed) or is_inbox_path(f"{norm}/x.md"):
        raise ValueError(f"folder pick {pick!r} lands in the inbox")
    return norm


def pick_batch_folders(briefs: list[tuple[str, str]]) -> dict[int, str]:
    """A folder for each unit of a /nucleate batch, from ONE call that sees
    every unit (ADR-0133). `briefs` are (name, text) per unit; the result maps
    a unit's index to its folder.

    Picked one at a time, the first unit's folder was an existing folder by the
    second pick: six papers on four subjects all went to `machine-learning`, the
    folder the first one invented (2026-10-26). A unit whose line is missing,
    unreadable or lands in the inbox is left out, and the caller picks for it
    alone.
    """
    from silica.model.llm import call_llm
    from silica.kernel.base.manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    listed = _listed_folders(inbox)
    # shortcut: 24,000 characters of briefs in all, 300 to 1500 per unit: 74
    # books come to ~6k tokens beside the folder list, far under the 64k
    # context. Past 80 units the floor grows the prompt past that sum; split
    # the batch into calls if a batch over 200 units ever appears.
    per = max(300, min(1500, 24000 // max(1, len(briefs))))
    sources = "\n\n".join(f"[{i}] {name}\n{text[:per]}".rstrip()
                          for i, (name, text) in enumerate(briefs, 1))
    prompt = (
        "Pick a destination folder in a knowledge vault for each source below; "
        "the sources are nucleated together. Group them by subject, broad "
        "enough that sources which build on one another or share a field's "
        "subarea land together: most folders should hold two or more of these "
        "sources, and a source that shares nothing with the others gets a "
        "folder of its own. One folder for all of them is right only when they "
        "all share one subject. A folder names a subject, never one source: no "
        "folder named after a source's title. The folders form one tree: one "
        "name and one place for each field. Reuse an existing folder when its "
        "subject is the source's subject; otherwise name a new one, at the top "
        "level or inside an existing folder (Parent/Subject). The last part of "
        "a path titles the folder's hub note, so it names the subject on its "
        "own: `Organic Chemistry/Reaction Mechanisms`, not `Organic "
        "Chemistry/Mechanisms`.\n"
        "Reply with a first line `Subjects: <folder>; <folder>; ...`, then one "
        "line per source, `<number>: <folder path>`, and nothing else.\n\n"
        "Existing folders:\n" + _folders_listing(listed) + "\n\nSources:\n\n" + sources
    )
    # reasoning=False as in pick_target_folder; ~12 tokens per answer line.
    # temperature=0: at the default, two calls on the same six papers grouped
    # them as four folders and as six (2026-10-27).
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}],
                    max_tokens=2048 + 16 * len(briefs), temperature=0, reasoning=False)
    picks: dict[int, str] = {}
    known = list(listed)
    for line in (resp.text or "").splitlines():
        m = re.match(r"\s*(?:[-*]\s*)?[\[(]?(\d+)[\])]?\s*[:.\-]?\s*(.+)", line)
        if not m:
            continue
        i = int(m.group(1)) - 1
        if not 0 <= i < len(briefs) or i in picks:
            continue
        pick = m.group(2).strip().strip('"`*').rstrip("/")
        try:
            picks[i] = _resolve_pick(pick, known, inbox)
        except ValueError:
            continue
        # A new folder joins the list, so the same subject spelt two ways in
        # one reply ("NLP", "nlp") lands in one folder.
        if picks[i] not in known:
            known.append(picks[i])
    return picks


def _unit_brief(label: str, mfs: list[str]) -> tuple[str, str]:
    """A converted unit as the batch pick reads it: its name and its first
    content segment. A references chunk would brief a paper by its
    bibliography."""
    from silica.kernel.forms import read_source_text
    from silica.kernel.base import frontmatter
    from silica.sources.convert import is_skippable_chunk

    first = next((f for f in mfs if not is_skippable_chunk(f)), mfs[0])
    try:
        body = frontmatter.split(read_source_text(first))[2]
    except Exception:
        # The Coordinator reports an unreadable file; here the name alone still
        # places the unit.
        body = ""
    return label or Path(first).name, body.strip()


def _pending_brief(src: str) -> tuple[str, str]:
    """A unit not yet converted: its file name, and the PDF's own Title and
    Author when it carries them (117 of 260 PDFs of the paper library do,
    2026-10-27). Its text is minutes of OCR away, and waiting for it would stop
    the next book converting while this one distills."""
    name = Path(src).name
    if Path(src).suffix.lower() != ".pdf":
        return name, ""
    try:
        from silica.sources import pdf_extract
        from silica.sources.convert import resolve_input

        meta = pdf_extract.metadata(resolve_input(src))
    except Exception:
        # A PDF that will not open is convert's to report, a few lines later;
        # here it only goes without its title.
        return name, ""
    return name, " / ".join(v for k in ("Title", "Author") if (v := meta.get(k)))


def _stage_envelope(body: str, stem: str, inbox: str) -> str:
    """Put one rendered conversation in the vault inbox for the FSM.

    The WAL lives outside the vault on purpose, but the FSM reads its sources
    through the driver, vault-relative (`to_vault_relative`), so the drain
    stages the rendered prose the way /web stages its findings: zero-trust
    ingress lands in the inbox, the gate decides what survives. The staged file
    is discarded by `_discard_staged` once the run is over, so the conversation
    never becomes a vault resident. Returns the vault-relative staged path.
    """
    from silica.driver import DRIVER

    from silica.kernel.write.preserve import Loss

    rel = f"{inbox}/{stem}.md"
    DRIVER.upsert(rel, body, loss=Loss.STAGED)
    return rel


def _episodic_distill(content: str, envelope: dict, *, run_id: str,
                      target: str) -> bool:
    """Harvest facts from one of Silica's own sessions. Writes no note.

    Machine memory enters the vault only by explicit promotion, so this branch
    keeps the distiller and throws its note body away: `ephemerals` is the whole
    harvest, and it lands in the episodic store like any other run's. Nothing is
    staged either — the transcript never becomes a vault file, not even one that
    gets deleted afterwards.
    ponytail: linear, no FSM — no chunk steering, no per-chunk retry, no write
    gate, because nothing is written. A failure leaves the envelope pending and
    the next drain repeats the call.
    """
    from silica.kernel import prep_delegation
    from silica.kernel.partition import partition_by_concepts
    from silica.kernel.recall.episodic import (
        EpisodicStore,
        capture_from_distill,
        key_vocabulary_section,
    )
    from silica.kernel.base.home import vault_digest
    from silica.kernel.text.keyphrase import extract_keyphrases
    from silica.kernel.text.payload import build_concept_entry

    concepts = [c.phrase for c in
                extract_keyphrases(content, lang=CONFIG.cooccurrence_lang)]
    if not concepts:
        return True  # a conversation with nothing to name has nothing to remember
    # Assembled here rather than through build_payload, which reads its source
    # back through the driver — i.e. would require this conversation to be a
    # vault file first. No collision search either: the vault is not the
    # destination, so the hits would only plan note edits this branch discards.
    payload = {"schema_version": 1, "batches": [{
        "inbox_file": f"{run_id}.md",
        "concepts": [
            build_concept_entry(name=c, inbox_content=content, collision=None,
                                in_new_concepts=True, window=450)
            for c in sorted(concepts)
        ],
    }]}
    seen = (envelope.get("captured_at") or "")[:10]
    ok = True
    for chunk in partition_by_concepts(payload, 7) or [payload]:
        # ADR-0021: the established keys, so the distiller reuses them instead
        # of coining a synonym per session — a chain that never lands twice on
        # the same key never reaches min_days, and the promotion queue stays
        # empty by construction. Re-read per chunk: the previous chunk's facts
        # are already in the store. Only this section, never build_substrate:
        # the vault's related notes have no business in a machine-memory prompt.
        result = prep_delegation.run_distiller(
            payload=chunk, target=target, session_date=seen,
            substrate=key_vocabulary_section(EpisodicStore()),
            # This lane keeps only ephemerals — never generate note bodies.
            structure_only=True,
        )
        if result.get("error"):
            logger.warning("drain: episodic distill failed (%s)", result["error"])
            ok = False
            continue
        # The run id is the envelope name: one session is one run, which is
        # exactly the unit `nucleation_candidates` counts. Note attribution is
        # session-level on purpose — every fact of an envelope carries the same
        # list. ponytail: per-fact attribution if the graph overlay gets noisy.
        capture_from_distill(result, run_id=run_id, seen=seen,
                             vault=vault_digest(CONFIG.vault_path),
                             notes=list(envelope.get("notes_touched") or []))
    return ok


def _discard_staged(rel: str) -> None:
    """Remove a staged transcript from the vault, wherever the run left it.

    A successful FSM run archives the source, so both paths are tried: the point
    is that no conversation text stays in the vault after the drain, archived or
    not. `archive_path_for` and not a spelled folder: `done/<name>` missed the
    `Done/` archive on a case-sensitive filesystem, and the miss went unseen.
    """
    from silica.driver import DRIVER
    from silica.kernel.base.manifest import archive_path_for

    for candidate in (rel, archive_path_for(rel)):
        try:
            DRIVER.delete(candidate)
        except Exception:
            # One of the two is always absent: the run either archived the
            # source or left it, and the other delete has nothing to find.
            continue


# Terminal FSM verdicts that leave nothing to retry: notes written, nothing
# worth writing, or this source already committed by an earlier run. Anything
# else — "partial", "failed", or no verdict at all — keeps the envelope pending.
# The FSM's verdict is the criterion, not `context["error"]`: best-effort phases
# record an error and carry on to Success (orchestrator._on_step_error).
_DRAIN_SETTLED = {"Success", "no_ops", "already_nucleated"}
# Where a drain's notes go when the folder pick fails. A fixed folder and not a
# stop, as `promote` has: nobody is there to finish it, and the staged transcript
# has to leave the vault with this run.
_DRAIN_FALLBACK_DIR = "Sessions"


def drain_wal(console) -> str:
    """`/nucleate` with no argument: drain this vault's capture WAL.

    A batch at a time (`collect`'s cap), so a 500-conversation import backlog
    becomes deliberate, resumable runs instead of one LLM bill.
    """
    import silica.capture as capture

    vault = CONFIG.vault_path.strip()
    if not vault:
        return "No vault is configured, so there is nothing to drain. Say so in one line."

    capture.housekeep(vault)
    envelopes, remaining = capture.collect(vault)
    if not envelopes:
        console.print("  nothing captured to drain.")
        return ""

    from silica.kernel.base.manifest import active_inbox_dir
    from silica.sources.transcript import SOURCE_SILICA, render
    inbox = active_inbox_dir() or "Inbox"
    staged: dict = {}
    episodic = 0
    for env_path in envelopes:
        try:
            envelope = json.loads(env_path.read_text(encoding="utf-8"))
            body = render(envelope)
            # Own sessions never take the note path (spec §11), so they are
            # never staged: no vault write, nothing to undo afterwards.
            rel = ("" if not body or envelope.get("source") == SOURCE_SILICA
                   else _stage_envelope(body, env_path.stem, inbox))
        except Exception as exc:
            logger.warning("drain: unreadable envelope %s (%s)", env_path.name, exc)
            capture.mark_failed(env_path)
            continue
        if not body:
            capture.mark_processed(env_path)  # nothing said, nothing to keep
            continue
        if rel:
            staged[env_path] = rel
            continue
        episodic += 1
        if _episodic_distill(body, envelope, run_id=env_path.stem, target=inbox):
            capture.mark_processed(env_path)
        else:
            remaining += 1  # pending: the next drain repeats the call
    if episodic:
        console.print(f"  drain: {episodic} session(s) → episodic memory")
    if not staged:
        if not episodic:
            console.print("  nothing captured to drain.")
        elif remaining:
            console.print(f"  {remaining} envelope(s) still pending, run /nucleate again")
        return ""

    files = list(staged.values())
    try:
        target_dir = pick_target_folder(files)
    except Exception as exc:
        logger.debug("drain: auto-target pick failed (non-fatal): %s", exc)
        target_dir = _DRAIN_FALLBACK_DIR
    console.print(f"  drain: {len(files)} conversation(s) → [bold]{target_dir}[/]")

    try:
        result, _ = distill(files, target_dir, keep_sources=False)
    finally:
        # Unstaging is not part of the happy path: a crash or a Ctrl+C must not
        # leave a raw conversation sitting in a committable vault.
        for rel in staged.values():
            _discard_staged(rel)
    # ponytail: batch-level outcome — one failed chunk leaves the whole batch
    # pending, and the next run re-drains it (the FSM's own dedup absorbs the
    # repeat). Per-envelope status if a mixed batch ever costs a real re-run.
    ok = result.get("final_status") in _DRAIN_SETTLED
    for env_path in staged:
        if ok:
            capture.mark_processed(env_path)

    status = result.get("final_status") or result.get("error") or "done"
    left = remaining + (0 if ok else len(staged))
    tail = f" — {left} envelope(s) still pending, run /nucleate again" if left else ""
    console.print(f"  drain finished: [bold]{status}[/]{tail}")
    return ""


def promote(keys: list[str], console) -> str:
    """`/promote [<key>]` — the consent bridge out of episodic memory.

    Bare: list what the store thinks is worth keeping. With a key: render that
    chain into a stub and send it through the ordinary nucleate gate, which is
    the point — machine memory earns a note the same way any other source does.
    """
    from silica.kernel.recall.episodic import EpisodicStore, entity_key

    store = EpisodicStore()
    if not keys:
        candidates = store.nucleation_candidates()
        if not candidates:
            console.print(
                "  No episodic candidates yet — nothing has come up in enough "
                "sessions to be worth a note."
            )
            return ""
        groups: dict[str, list] = {}
        for c in candidates:
            groups.setdefault(entity_key(c.key), []).append(c)
        console.print(f"  {len(groups)} episodic candidate(s):")
        for ent, members in sorted(groups.items()):
            attrs = " · ".join(f"{m.key.rsplit('.', 1)[-1]}={m.text}" for m in members)
            # the busiest attribute stands for the entity. The union
            # of days would need every chain re-walked for one console line.
            days = max(m.day_count for m in members)
            since = min(m.since for m in members)
            console.print(f"  · [bold]{ent}[/] — seen on {days} days since {since}: {attrs}")
        console.print("  Promote one with [bold]/promote <key>[/].")
        return ""

    from silica.driver import DRIVER
    from silica.kernel.recall.episodic import promotion_stub
    from silica.kernel.base.manifest import active_inbox_dir

    # The key names an attribute, the promotion writes the entity it belongs to:
    # `user.dog.name` and `user.dog.breed` are one note about one dog.
    key = keys[0]
    entity = entity_key(key)
    heads = sorted((f for f in store.live_facts() if entity_key(f.key) == entity),
                   key=lambda f: f.key)
    if not heads:
        console.print(
            f"  No live episodic chain for [bold]{key}[/] — run /promote to list them."
        )
        return ""
    done = next((h for h in heads if h.promoted), None)
    if done is not None:
        console.print(
            f"  [bold]{entity}[/] is already promoted to {done.promoted} — edit that "
            "note, or /nucleate it again to refresh it."
        )
        return ""

    inbox = active_inbox_dir() or "Inbox"
    # The key is model-authored (distiller output), and here it names a file:
    # keep it to one path segment so no key can stage outside the inbox.
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", entity).strip(".-") or "episodic"
    rel = f"{inbox}/{stem}.md"
    from silica.kernel.write.preserve import Loss

    DRIVER.upsert(rel, promotion_stub(heads, store=store), loss=Loss.STAGED)
    try:
        target_dir = pick_target_folder([rel])
    except Exception as exc:
        # No invented folder: the stub is already in the inbox, so the user
        # finishes it with the ordinary verb rather than losing the render.
        logger.debug("/promote: auto-target pick failed: %s", exc)
        console.print(
            f"  Could not pick a folder. The stub is at [bold]{rel}[/] — "
            f"run /nucleate {rel} --target=<folder>."
        )
        return ""

    console.print(f"  promote: [bold]{entity}[/] → {target_dir}")
    # episodic_capture off: the stub is a render of the store, so distilling it
    # back in would nest the chain inside itself once per promotion.
    # promotion lens: the stub is finished verbatim content — the default
    # authoring lens + 275-char floor rejected every real promotion (55/155/34
    # chars, all no_ops), and the extractive lens skipped every fact as
    # "time-bound personal" (the ingest-direction diversion). The promotion
    # lens selects verbatim, one note per entity, and never re-emits
    # ephemerals; the gate enforces extractivity at the lower floor.
    result, _ = distill([rel], target_dir, keep_sources=False, episodic_capture=False,
                        profile="promotion")
    status = result.get("final_status") or result.get("error") or "done"
    # The FSM names the note, so the ledger CLEANUP appends is the only place
    # the path can be read back from. Last record wins: a re-promotion of the
    # same key overwrites the stamp with the newer note.
    from silica.kernel.write.provenance import read_records

    from silica.kernel.base.manifest import source_key

    notes = list((read_records(source_key(rel)) or [{}])[-1].get("notes") or [])
    # CLEANUP's record lists the run's hub note first ("Life/Life"): the stamp
    # must name the note that holds the facts, not the folder's MOC.

    hub_stem = Path(target_dir.rstrip("/")).name
    entity_notes = [n for n in notes
                    if Path(n).name.removesuffix(".md") != hub_stem]
    notes = entity_notes or notes
    if notes:
        # Re-read: another chunk of the run may have written this store, so the
        # pre-run snapshot in `store` is stale and saving it would erase that.
        ids = [h.id for h in heads]
        store = EpisodicStore()
        # By chain, not by id: the run may have superseded a fact being
        # promoted, and the stamp belongs to the chain, so it follows the head.
        head_of = {link.id: f for f in store.live_facts() for link in store.chain(f)}
        stamped = {head_of[i].id: head_of[i] for i in ids if i in head_of}
        for head in stamped.values():
            head.promoted = notes[0]
        if not stamped:
            console.print(f"  wrote {notes[0]}, but the chains for [bold]{entity}[/] "
                          "are gone from the store — not stamped.")
            return ""
        store.save()
        console.print(
            f"  promoted: [bold]{entity}[/] ({len(stamped)} chain(s)) → {notes[0]}")
    else:
        console.print(f"  promote finished: [bold]{status}[/] — nothing written, "
                      "the chain stays in the queue.")
    return ""


def rejoin_spaced_paths(tokens: list[str]) -> list[str]:
    """Re-join adjacent tokens that only exist as one spaced path.

    An unquoted `/nucleate Inbox/LLM Agent Memory.pdf` shlex-splits into
    three tokens, and each then failed separately with an error naming the
    wrong file ("Skipped Memory.pdf"). Greedy longest-join: a token that
    does not exist on its own but does exist joined with its neighbours
    (vault-relative or cwd-relative) becomes that one path. Tokens that
    exist alone are never merged, so quoted paths keep working unchanged.
    """
    def _exists(s: str) -> bool:
        vp = CONFIG.vault_path.strip()
        return Path(s).exists() or bool(vp and (Path(vp) / s).exists())

    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") or _exists(tok):
            out.append(tok)
            i += 1
            continue
        joined = None
        cand = tok
        for j in range(i + 1, len(tokens)):
            if tokens[j].startswith("-"):
                break
            cand = f"{cand} {tokens[j]}"
            if _exists(cand):
                joined = (j, cand)
        if joined:
            out.append(joined[1])
            i = joined[0] + 1
        else:
            out.append(tok)
            i += 1
    return out


def distill(files: list[str], target_dir: str, *, keep_sources: bool, hub: str = "", seen: str = "",
            profile: str = "", resume_run_id: str = "", cancel_token=None,
            episodic_capture: bool = True) -> tuple[dict, str | None]:
    """The one entry into the Coordinator: the run's result, and its run id.

    `keep_sources` has no default here, because the Coordinator's is False: every
    way in says what it wants, and the drain and a promotion mean False. It was
    set only on the CLI side once, so the same file nucleated through the agent
    tool silently produced notes whose source could never be checked.
    """
    from silica.router.coordinator import Coordinator

    coordinator = Coordinator(
        inbox_files=files, target_dir=target_dir, hub=hub or None, keep_sources=keep_sources,
        seen_override=seen or None, distill_profile=profile or None,
        resume_run_id=resume_run_id or None, cancel_token=cancel_token,
        episodic_capture=episodic_capture,
    )
    result = coordinator.run()
    return result, getattr(getattr(getattr(coordinator, "fsm", None), "progress", None), "run_id", None)


def project_result(result: dict, run_id: str | None, files_total: int,
                   skipped: int = 0) -> dict[str, Any]:
    """What a Coordinator run returns to its caller: the injector tool's result,
    and the one a typed /nucleate hands the chat's block."""
    # Agent-facing projection: outcomes only, never the raw FSM context. The
    # raw context once fed a confabulated success report — planned concepts in
    # payload.chunks read as "created notes", and a last-write-wins error field
    # hid 5 of 6 batch failures.
    failed = result.get("failed_chunks", [])
    projected: dict[str, Any] = {
        "final_status": result.get("final_status", "unknown"),
        "run_id": run_id,
        "chunks_committed": result.get("committed_chunks", 0),
        "chunks_failed": len(failed),
        "failed_chunks": failed,
        "files_summary": result.get("files_summary", []),
        "subagents": result.get("subagents", {}),
        # What the run actually yielded. Both frontends build their completion
        # line from these; until they were projected here the keys existed only
        # inside fsm.context and every summary read "1 file · 3m12s", never the
        # note and link counts it was written to show.
        "yield_notes": result.get("yield_notes", 0),
        "yield_links": result.get("yield_links", 0),
        "files_total": files_total,
        "apparatus_skipped": skipped,
    }
    if result.get("error"):
        projected["error"] = result["error"]
    return projected


def split_apparatus(files: list[str]) -> tuple[list[str], list[str]]:
    """(content, apparatus): the files worth distilling, and the ones that only
    surround a work. Apparatus is not content, and both ways in filter it: a
    filter that lived only in the CLI was no filter at all, since the injector
    tool is what the web drag-drop takes, and a references list or a venue
    checklist arriving there was distilled into venue/journal/ethics notes. The
    raw file stays in the inbox for lookup."""
    from silica.sources.convert import is_skippable_chunk

    apparatus = [f for f in files if is_skippable_chunk(f)]
    return [f for f in files if f not in apparatus], apparatus


def run_injector(files: list[str], target_dir: str, *, hub: str = "", resume_run_id: str = "",
                 keep_sources: bool = True, cancel_token=None) -> dict[str, Any]:
    """The agent tool's way in: convert what no adapter claims, drop the apparatus,
    run, and project the result."""
    # The FSM re-reads inbox files as prose, so a file no adapter claims (PDF,
    # EPUB, other binaries) has to be transcoded first. `/nucleate` has always
    # done that inline; the agent tool used to answer "ask the user to run
    # /convert", which in a REPL means telling the user to type, themselves, the
    # thing they just asked for — and in a library of 74 scanned books that
    # instruction stands between the user and the entire corpus.
    from silica.kernel.base.manifest import get_active_manifest
    from silica.sources.registry import adapter_for

    enabled = get_active_manifest().sources
    if any(adapter_for(f, enabled=enabled) is None for f in files):
        from silica.sources.convert import convert

        resolved: list[str] = []
        rejected: list[str] = []
        for f in files:
            if adapter_for(f, enabled=enabled) is not None:
                resolved.append(f)
                continue
            try:
                resolved.extend(convert(f, dest_dir=target_dir))
            except (ValueError, RuntimeError) as exc:
                rejected.append(f"{f} ({exc})")
        if rejected:
            return {"error": f"Not ingestible as-is: {'; '.join(rejected)}."}
        files = list(dict.fromkeys(resolved))
        if not files:
            return {"error": "Conversion produced no markdown to nucleate."}

    kept, apparatus = split_apparatus(files)
    if not kept:
        return {"error": "Only apparatus sections (references, contents, venue "
                         "checklists) were given — nothing to nucleate. They stay "
                         "in the inbox for lookup."}
    # keep_sources on by default, matching /nucleate. The leaf lives in `sources/`,
    # which is retrieval-invisible by construction, so it costs disk and nothing
    # else, and it is what makes a note's verbatim source reachable at all
    # (reliability_tier reads exactly that).
    result, run_id = distill(kept, target_dir, hub=hub, resume_run_id=resume_run_id,
                             keep_sources=keep_sources, cancel_token=cancel_token)
    return project_result(result, run_id, len(kept), len(apparatus))


# --- /nucleate: one phase per function --------------------------------------
# The lane is glob -> folder -> plan -> dispatch. Each phase names its inputs and
# its outputs instead of reaching into 400 lines of shared locals, so a change to
# (say) folder expansion no longer has to be read against the dispatch loop.


class _NucleatePlan(NamedTuple):
    """What one /nucleate invocation resolved its arguments into."""

    ready_units: list[tuple[str, list[str]]]   # (label, md files) distillable now
    to_convert: list[str]                      # sources still owed a conversion
    staged: int                                # code notes written inline, no LLM
    needs_agent: bool                          # an argument the flag parser could not read
    # pdf path -> (leaf basename, identity) for a Zotero paper (ADR-0055)
    leaf_for: dict[str, tuple[str, dict]] = {}


def _nucleate_globs(files: list[str], console) -> tuple[list[str], bool]:
    """Expand glob tokens against the vault root, and report whether one missed.

    A miss is an answer, not a question for the agent, so the caller stops on it.
    """
    # B6: a glob token ("Inbox/*", the README's documented batch form) is
    # expanded here, against the vault root. Unexpanded it would survive as
    # a literal and fall through to the agent, taking the one batch command
    # the docs advertise off the deterministic path.
    import glob as _glob
    globbed: list[str] = []
    glob_miss = False
    for f in files:
        if any(ch in f for ch in "*?["):
            hits = sorted(
                h.replace(os.sep, "/")
                for h in _glob.glob(f, root_dir=CONFIG.vault_path.strip() or ".")
            )
            if hits:
                console.print(f"  {f}: [bold]{len(hits)}[/] match(es)")
            else:
                console.print(f"  [yellow]{f}: no files match[/]")
                glob_miss = True
            globbed.extend(hits)
        else:
            globbed.append(f)
    files = globbed
    return files, glob_miss


def _nucleate_expand_folders(
    files: list[str], enabled, console
) -> tuple[list[str], dict[str, str]]:
    """Expand folder arguments to the files under them.

    Also returns `run_root`: the folder each code file came from, which is how
    the code lane names its destination.
    """
    from silica.sources.registry import adapter_for, expand_folder, folder_rel
    from silica.kernel.recall.listing import notes_under

    # A folder argument is the common way to say "this subsystem": expand it
    # to the source files under it, then dispatch each exactly like a file.
    # `run_root` remembers which folder each file came from — the code lane
    # names its destination folder after it.
    expanded: list[str] = []
    run_root: dict[str, str] = {}
    for f in files:
        adapter = adapter_for(f, enabled=enabled)
        group = expand_folder(f, enabled) if adapter is None else []
        if group:
            console.print(f"  {f}: [bold]{len(group)}[/] source file(s)")
            # run_root is the code lane's destination naming, so it stays
            # keyed on code files only.
            run_root.update(dict.fromkeys(group, folder_rel(f) or ""))
        elif adapter is None:
            # A folder of notes. `expand_folder` cannot see one (git-backed
            # census, and a plain vault is no repo), so this used to fall
            # through to the agent fallback below with nothing but the
            # folder name — a listing an LLM had to guess at.
            group = notes_under(f)
            # An inbox folder of PDFs answered "0 notes" and went to the
            # agent as an unresolvable name. Unconverted files are
            # first-class /nucleate input (each runs through convert below),
            # so a folder argument picks them up like it picks up notes.
            from silica.kernel.base.formats import DOC_EXTS, IMG_EXTS
            from silica.kernel.recall.listing import unconverted_under
            pending = [
                p for p in unconverted_under(f)
                if Path(p).suffix.lower() in DOC_EXTS
            ]
            # Images stay batch input only inside the inbox (screenshots
            # dropped there to OCR). In a SOURCE folder they are a book
            # photographed page by page: converting each page as its own
            # document is hundreds of one-page runs of garbage.
            from silica.kernel.base.layout import is_inbox_path
            if not is_inbox_path(f.rstrip("/") + "/x.md"):
                photos = [p for p in pending
                          if Path(p).suffix.lower() in IMG_EXTS]
                if photos:
                    pending = [p for p in pending if p not in photos]
                    console.print(
                        f"  [dim]{f}: left {len(photos)} image(s) alone — "
                        f"a photographed book is one artefact, not "
                        f"{len(photos)} documents; /nucleate an image "
                        f"explicitly to OCR it on its own[/]"
                    )
            # A converted document's own figures are not sources: 14 lectures
            # in Inbox/machine_learning queued the 129 images/ they embed as
            # 129 more runs (2026-10-12). What a note of this batch embeds
            # stays put; a screenshot no note embeds is still OCR input.
            paths, names = _embedded_in(group)
            figures = [p for p in pending
                       if p in paths or Path(p).name.casefold() in names]
            if figures:
                pending = [p for p in pending if p not in figures]
                console.print(f"  [dim]{f}: left {len(figures)} file(s) "
                              f"its notes embed alone[/]")
            # A document converted earlier sits beside its notes, and those
            # notes ride the document's own unit (reused segments, in
            # plan_units). Listed here too, every lecture of a course
            # also joined one loose-note unit, so each was queued twice.
            carried = _converted_notes_of(pending)
            if carried:
                group = [g for g in group if g not in carried]
            if group or pending:
                detail = f"[bold]{len(group)}[/] note(s)" if group else ""
                if pending:
                    detail += (", " if detail else "") + \
                        f"[bold]{len(pending)}[/] file(s) to convert"
                console.print(f"  {f}: {detail}")
            group = group + pending
        expanded.extend(group or [f])
    files = list(dict.fromkeys(expanded))
    return files, run_root


def _converted_notes_of(sources: list[str]) -> set[str]:
    """The inbox notes an earlier convert() made from these source files."""
    if not sources:
        return set()
    from silica.sources.convert import resolve_input

    prior = prior_conversions()
    out: set[str] = set()
    for s in sources:
        try:
            out.update(prior.get(str(resolve_input(s)), {}).get("inbox") or [])
        except ValueError:
            continue  # gone since the census: convert() reports it later
    return out


def _embedded_in(notes: list[str]) -> tuple[set[str], set[str]]:
    """What `notes` embed: vault-relative paths from `![](rel)` (resolved
    against the note's folder) and the names in `![[name]]`, which Obsidian
    resolves by name anywhere in the vault, casefolded."""
    import posixpath
    from urllib.parse import unquote

    from silica.kernel.base.syntax import EMBEDDED_ADDRESS_RE, EMBEDDED_NAME_RE

    paths: set[str] = set()
    names: set[str] = set()
    root = Path(CONFIG.vault_path)
    for note in notes:
        try:
            text = (root / note).read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A note gone between the listing and this read embeds nothing;
            # its figures then convert as before, which is today's behaviour.
            continue
        base = posixpath.dirname(note)
        for target in EMBEDDED_ADDRESS_RE.findall(text):
            if "://" not in target:
                paths.add(posixpath.normpath(posixpath.join(base, unquote(target))))
        names.update(t.strip().rsplit("/", 1)[-1].casefold() for t in EMBEDDED_NAME_RE.findall(text))
    return paths, names


def _nucleate_zotero(
    files: list[str], enabled, undo_run, console
) -> tuple[list[str], dict[str, tuple[str, dict]], int]:
    """Zotero targets resolved to the PDFs they name (ADR-0055 decision 4).

    A paper with a PDF becomes that PDF, so the converter queue, its batch
    resume and its "already nucleated" skip treat it like a dropped file; its
    leaf rides in `leaf_for`. A paper with no PDF is written as its leaf here,
    from title, authors and abstract.
    """
    from silica.sources import zotero
    from silica.sources.registry import adapter_for, stage

    out: list[str] = []
    leaf_for: dict[str, tuple[str, dict]] = {}
    staged = 0
    for f in files:
        if adapter_for(f, enabled=enabled) is not zotero.ZOTERO:
            out.append(f)
            continue
        try:
            # importing_client, not client: a paper with a PDF never reaches
            # ZoteroAdapter.read, so the owner check has to stand here too.
            papers = zotero.resolve(zotero.importing_client(), f)
        except zotero.ZoteroError as exc:
            console.print(f"  [yellow]{f}: {exc}[/]")
            continue
        known = zotero.vault_leaves()
        for paper in papers:
            if paper.pdf is None:
                result = stage(zotero.ZOTERO, f"zotero://select/library/items/{paper.key}",
                               "", undo_run)
                if result["status"] == "ok":
                    staged += 1
                    console.print(f"  {f}: no PDF in Zotero, wrote [bold]{result['note_path']}[/]")
                else:
                    console.print(f"  [yellow]{f}: {result.get('message', '')}[/]")
                continue
            pdf = str(paper.pdf)
            out.append(pdf)
            leaf_for[pdf] = (known.get(paper.key, paper.leaf_name), paper.fields)
    return out, leaf_for, staged


def triage(target: str, enabled, run_root: str = "", undo_run: str | None = None) -> dict | None:
    """What a file's source does with it: None when no source claims it, so it is
    the converter's; else the status `stage` reports, `distill` for prose that
    stays as it is and `ok` for a stub already written."""
    from silica.sources.registry import adapter_for, stage

    adapter = adapter_for(target, enabled=enabled)
    return None if adapter is None else stage(adapter, target, run_root, undo_run)


def owed(files: list[str]) -> list[str]:
    """The files of a /nucleate that name one document each and still need a
    conversion: no source claims them, and no earlier conversion of the same
    file sits in the inbox or in done/ (the batch resume of `plan_units`).
    [] when one of them is a glob or a folder: the whole command runs as before."""
    from silica.kernel.base.formats import DOC_EXTS
    from silica.kernel.base.manifest import get_active_manifest
    from silica.sources.convert import resolve_input
    from silica.sources.registry import adapter_for

    enabled = get_active_manifest().sources
    out: list[str] = []
    converted: dict | None = None
    for f in files:
        if any(ch in f for ch in "*?["):
            return []
        try:
            src = resolve_input(f)
        except ValueError:
            continue  # a Zotero key, or a missing file: the command says which
        if src.is_dir():
            return []
        if src.suffix.lower() not in DOC_EXTS or adapter_for(f, enabled=enabled) is not None:
            continue  # a note or code: staged by the command, no conversion
        converted = prior_conversions() if converted is None else converted
        seen = converted.get(str(src))
        if not (seen and (seen["inbox"] or seen["done"])):
            out.append(f)
    return out


def plan_units(
    files: list[str], enabled, run_root: dict[str, str], undo_run, console
) -> _NucleatePlan:
    """Triage every resolved file into staged-now, ready-to-distill, or to-convert."""
    files, leaf_for, staged = _nucleate_zotero(files, enabled, undo_run, console)
    md_files: list[str] = []
    needs_agent = not files  # only flags given (dropped --folder=) → agent infers
    converted: dict[str, dict] | None = None  # built on first need
    # Pipeline units: loose .md arguments form one ready unit; each source
    # document is its own unit — ready when its segments already exist,
    # queued for conversion otherwise. One Coordinator run per unit, so
    # book N+1 can convert while book N distills.
    ready_units: list[tuple[str, list[str]]] = []
    to_convert: list[str] = []
    for f in files:
        result = triage(f, enabled, run_root.get(f, ""), undo_run)
        if result is None:
            # No source claims this file type → the converter lane (PDF
            # today). A bare name or folder (no suffix) is a resolvable
            # intent the flag parser couldn't read — agent, not converter.
            if not Path(f).suffix:
                needs_agent = True
                continue
            # Batch resume: conversion is the expensive half (minutes of
            # OCR per scanned book), so a re-run must not pay it again.
            # Both identities are already on disk — segments in the inbox
            # (interrupted run) and the done/ archive (finished book) both
            # carry `source_file` frontmatter.
            from silica.sources.convert import resolve_input
            try:
                src_key = str(resolve_input(f))
            except ValueError:
                src_key = ""  # convert() stays the authority on missing files
            if converted is None:
                converted = prior_conversions()
            prior = converted.get(src_key) if src_key else None
            if prior and prior["inbox"]:
                console.print(
                    f"  {f}: reusing [bold]{len(prior['inbox'])}[/] "
                    f"already-converted segment(s)"
                )
                ready_units.append((f, prior["inbox"]))
                continue
            if prior and prior["done"]:
                console.print(
                    f"  [dim]{f}: already nucleated "
                    f"({prior['done']} segment(s) in done/) — skipped[/]"
                )
                continue
            to_convert.append(f)
            continue
        if result["status"] == "distill":
            md_files.append(f)
        elif result["status"] == "ok":
            staged += 1
            code_ref = result["meta"].get("code_ref", "")
            if len(files) <= 10:  # a whole subsystem would flood the terminal
                # Only a code stub has a code_ref; a mail thread printed "(code_ref )".
                ref = f" (code_ref {code_ref[:8]})" if code_ref else ""
                console.print(f"  Wrote [bold]{result['note_path']}[/]{ref}.")
        else:
            console.print(f"  [yellow]{f}: {result.get('message', '')}[/]")

    if staged:
        if len(files) > 10:
            console.print(f"  Wrote [bold]{staged}[/] code note(s). /wiki for prose.")
        console.print("  [dim]/revert undoes this run.[/]")

    # Loose .md arguments distill together, exactly as before the pipeline —
    # first in line: they are ready, so the first conversion overlaps them.
    # Converted segments among them are a unit per source, as a PDF argument
    # is: six papers sent as their inbox folder were one unit, so one folder
    # pick read off the first file filed all six (2026-10-26).
    if md_files:
        ready_units[:0] = _units_by_source(list(dict.fromkeys(md_files)))
    return _NucleatePlan(ready_units, to_convert, staged, needs_agent, leaf_for)


def _units_by_source(md_files: list[str]) -> list[tuple[str, list[str]]]:
    """Loose files as units: the ones with no `source_file` together under
    no label, then one unit per converted source, labelled with its name, in
    the order each source first appears."""
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter

    groups: dict[str, list[str]] = {}
    for f in md_files:
        try:
            data, _, _ = frontmatter.split(DRIVER.read_note(f).content or "")
            src = str((data or {}).get("source_file") or "") if isinstance(data, dict) else ""
        except Exception:
            src = ""  # unreadable here: the Coordinator reports it, in the loose unit
        groups.setdefault(src, []).append(f)
    loose = groups.pop("", [])
    units = [("", loose)] if loose else []
    return units + [(Path(src).stem, fs) for src, fs in groups.items()]


def prepare_unit(
    mfs: list[str], target_dir: str | None, profile: str | None, undo_run, console, ask=None
) -> list[str]:
    """refs filter → draft filing → provenance drop, for one unit."""
    # A folder arg can list both a PDF and its already-converted chunks;
    # convert() upserts the same chunk paths, so dedup keeps each once.
    mfs = list(dict.fromkeys(mfs))

    # Flagged chunks (convert marks them `references: true` / `boilerplate: true`)
    # and Excalidraw drawings: never a note made of a drawing's JSON.
    mfs, ref_chunks = split_apparatus(mfs)
    if ref_chunks:
        console.print(
            f"  [dim]skipped {len(ref_chunks)} non-content file(s) "
            f"(references, contents, venue checklists, a document's list of parts, Excalidraw drawings) — "
            f"kept in the inbox, not distilled into notes[/]"
        )
        if not mfs:
            console.print("  [yellow]nothing left to nucleate: only non-content files were given[/]")
            return []

    # Draft filing (docs/specs/nucleation-forms.md): the owner's own
    # working material is filed, not distilled. Runs before auto-target
    # so a run that was ONLY drafts never pays the folder-pick call. An
    # explicit --profile tops the ladder: no resolution, no filing.
    if not profile:
        mfs = file_drafts(mfs, target_dir or "", undo_run, console, ask)
        if not mfs:
            return []  # everything was filed; reported above

    from silica.kernel.base.manifest import source_key
    from silica.kernel.write.provenance import (
        already_distilled, check_renucleate, content_sha256,
    )

    kept_md: list[str] = []
    distilled_prior = 0
    for mf in mfs:
        try:
            incoming_sha = content_sha256(mf)
            if not incoming_sha:
                kept_md.append(mf)
                continue
            # Same sha as the last COMPLETE record with notes ⇒ this segment
            # is already in the vault; re-distilling it costs a full LLM
            # pass to write nothing (the failure cases are provenance's).
            if already_distilled(source_key(mf), incoming_sha):
                distilled_prior += 1
                continue
            modified, prior_notes = check_renucleate(source_key(mf), incoming_sha)
            if modified:
                console.print(
                    f"  [yellow]re-nucleate of a modified source: {prior_notes} note(s) "
                    f"derived from the previous version[/]"
                )
        except Exception as exc:
            logger.debug("/nucleate: re-nucleate provenance check skipped for %s (non-fatal): %s", mf, exc)
        kept_md.append(mf)
    if distilled_prior:
        console.print(
            f"  [dim]{distilled_prior} segment(s) already distilled "
            f"(unchanged since their run) — skipped[/]"
        )
    return kept_md


def _zotero_backlink(leaf: tuple[str, dict], run_id: str | None, console) -> None:
    """The paper's `Silica · <title>` note in Zotero, after its run: only then
    does the vault hold the notes it lists (ADR-0055 decision 7). Best-effort,
    since the vault side of the import has already succeeded."""
    from silica.kernel.base.manifest import get_active_manifest
    from silica.sources import zotero, zotero_write

    link = get_active_manifest().zotero
    if link is None or not link.backlinks:
        return
    try:
        status = zotero_write.backlink(zotero.client(), leaf[1]["zotero_key"],
                                       _leaf_rel(leaf[0]), run_id=run_id or "")
    except zotero.ZoteroError as exc:
        console.print(f"  [yellow]Zotero: back-link note not written: {exc}[/]")
        return
    if status == "edited":
        console.print("  Zotero: the back-link note was edited in Zotero, so it was left as it is")
    else:
        console.print(f"  Zotero: back-link note {status} · in zotero.jsonl · undo: /revert")


def _leaf_rel(name: str) -> str:
    from silica.kernel.base.layout import SOURCES_DIR
    from silica.kernel.base.manifest import in_write_dir

    return f"{in_write_dir(SOURCES_DIR)}/{name}"


def _leaf_text(name: str) -> str | None:
    from silica.driver import DRIVER

    try:
        return DRIVER.read_note(_leaf_rel(name)).content
    except Exception:
        return None  # no leaf yet: /revert deletes the one convert creates


def _nucleate_result_line(result: dict) -> str:
    """The one-line outcome of a Coordinator run, ready to print after the label.

    `label` inside the coverage comprehension is the counter's name, deliberately
    not the caller's unit label — extracting this is what stops the two sharing
    a name in one scope.
    """
    status = result.get("final_status") or result.get("error") or "done"
    failed = result.get("failed_chunks") or []
    extra = f" — {len(failed)} chunk(s) failed" if failed else ""
    sw = result.get("link_sweep") or {}
    if sw.get("links_stripped"):
        extra += (
            f" — {sw['links_stripped']} dangling link(s) unlinked "
            f"in {sw['notes_edited']} note(s)"
        )
    if sw.get("links_relinked"):
        extra += f" — {sw['links_relinked']} spelling variant(s) repointed"
    # Coverage, post-anneal. Silence here read as "everything landed",
    # and the counts that said otherwise lived in log.md and a bundle
    # under ~/.silica — neither of which anyone opens after a Success.
    cov = result.get("coverage") or {}
    bits = [
        f"{cov[k]} {label}" for k, label in (
            ("recovered_ops", "deferred op(s) recovered"),
            ("deferred_ops", "still deferred"),
            ("flagged_notes", "note(s) landed flagged for review"),
        ) if cov.get(k)
    ]
    if bits:
        extra += " — " + ", ".join(bits)
    # A re-nucleated source keeps its previous derivation beside the new one;
    # the line says so and names the way back, since no prompt stood in the way.
    for src, n in sorted((result.get("renucleated") or {}).items()):
        extra += (f" — {src} re-nucleated: {n} note(s) from its previous version "
                  f"kept (/revert --source {src} removes them)")
    return f"[bold]{status}[/]{extra}"


def nucleate(files: list[str], *, target_dir: str = "", hub: str = "", seen: str = "",
             profile: str = "", keep_sources: bool = True, user_input: str = "",
             console, ask=None) -> str | None:
    """Ingest sources into vault notes: triage them, then one Coordinator run per unit.

    Returns "" when it dispatched, or an agent-directed message when the target
    cannot be resolved from the arguments alone.
    """
    from silica.kernel.base.manifest import get_active_manifest
    from silica.sources.convert import GpuOutOfMemory, convert
    from silica.kernel.write.gate import open_run

    files, glob_miss = _nucleate_globs(files, console)
    if not files and glob_miss:
        return ""  # a miss is an answer, not a question for the agent

    enabled = get_active_manifest().sources
    files, run_root = _nucleate_expand_folders(files, enabled, console)

    # One CLI journal run per /nucleate invocation for what the planner writes
    # itself (staged code, filed drafts). The FSM opens its own journal run per
    # dispatched unit, so on a multi-book batch /revert undoes one book at a
    # time, most recent first — run it again for the previous one.
    undo_run = open_run("nucleate") if files else None
    ready_units, to_convert, staged, needs_agent, leaf_for = plan_units(
        files, enabled, run_root, undo_run, console)

    if not ready_units and not to_convert:
        if staged or not needs_agent:
            # Staged inline, or only genuinely-unsupported files — nothing for the agent.
            return ""
        # A dropped --folder=, a directory arg, or connective words the flag
        # parser can't read. Hand the raw line to the agent so it infers intent
        # (it already holds the tools + the vault map).
        return (
            f"The user typed {user_input!r} to nucleate/ingest, but no ingestible "
            "file was resolved. The argument may be a folder (call silica_files "
            "with folder= and nucleate both its notes and its \"code\" entries — "
            "a code folder holds no .md and is still ingestible), a single note, or carry a "
            "--target/--folder the flag parser missed. Resolve the inbox file(s), "
            "then call silica_run_injector with the resolved inbox_files and "
            "target_dir. If nothing is ingestible, say so briefly."
        )

    total_units = len(ready_units) + len(to_convert)
    batch = total_units > 1
    dispatched = 0

    def _at() -> str:
        """Clock on the per-unit lines of a batch — a folder of scanned
        books runs for hours, and the unit boundaries are the only place
        to read where the time went. Single-unit runs stay unadorned."""
        from datetime import datetime
        return f"[dim]{datetime.now().strftime('%H:%M:%S')}[/] " if batch else ""

    # Conversions run with the user-passed destination, as they always did:
    # an auto-picked target is resolved at first dispatch, after conversion.
    convert_dest = target_dir

    # Index → folder for a batch with no --target, from one call that sees
    # every unit (ADR-0133), made before the first conversion starts.
    picks: dict[int, str] = {}
    if batch and not target_dir:
        names = [Path(label or segs[0]).name for label, segs in ready_units] + [
            Path(src).name for src in to_convert]
        try:
            picks = pick_batch_folders(
                [_unit_brief(label, segs) for label, segs in ready_units]
                + [_pending_brief(src) for src in to_convert])
        except Exception as exc:
            # Each unit is then picked on its own, as before ADR-0133: a worse
            # grouping, never a lost run.
            logger.debug("/nucleate: batch folder pick failed (non-fatal): %s", exc)
        if picks:
            console.print(f"  auto-target: {len(picks)} of {total_units} source(s) → "
                          f"{len(set(picks.values()))} folder(s)")
            for k, folder in sorted(picks.items()):
                console.print(f"    {names[k]} → [bold]{_announced_target(folder)}[/]")

    def _dispatch_unit(label: str, mfs: list[str], pick: str | None = None) -> str | None:
        """Prepare and run one unit; a returned string is the agent
        fallback (unresolvable target) and ends the whole batch."""
        nonlocal dispatched
        mfs = prepare_unit(mfs, target_dir, profile, undo_run, console, ask)
        if not mfs:
            return None

        unit_target = target_dir or pick
        if not unit_target:
            # auto-target: one small folder-pick call for a unit the batch
            # call left out (or a single unit), not a full agent turn.
            try:
                unit_target = pick_target_folder(mfs)
                console.print(f"  auto-target: [bold]{_announced_target(unit_target)}[/]")
            except Exception as exc:
                logger.debug("/nucleate: auto-target pick failed (non-fatal): %s", exc)

        if not unit_target:
            # Fallback: hand the folder choice to the agent (legacy behavior).
            files_json = json.dumps(mfs)
            msg = (
                f"Run the Injector pipeline for {len(mfs)} file(s).\n"
                f"No target folder was given. Skim the inbox file(s) {files_json}, "
                f"then pick the single most relevant existing vault folder for "
                f"this content (use the vault map; list folders if unsure). If "
                f"nothing fits, pick a sensible new folder name. State the chosen "
                f"folder in one line, then call `silica_run_injector` with "
                f"inbox_files={files_json}, target_dir=<chosen folder>"
            )
            if hub:
                msg += f", hub={json.dumps(hub)}"
            return msg + "."

        # Direct FSM dispatch — no LLM orchestrator. The old path
        # round-tripped the whole session history through the model on
        # every turn just to relay these arguments to silica_run_injector
        # (~40% of a nucleate run's tokens for a handful of decision tokens).
        import time as _time

        from silica.obs import narration as _narr_mod
        from silica.obs.bus import BUS
        from silica.obs.events import ToolCompleteEvent, ToolErrorEvent, ToolStartEvent

        head = f"{label}: " if (batch and label) else ""
        console.print(
            f"  {_at()}{head}nucleate: {len(mfs)} file(s) → [bold]{_announced_target(unit_target)}[/]"
        )
        # The unit as the injector call the agent makes: the web chat drew no
        # block for it and dropped its phases, and the narration held no running
        # tool, so the Chats mark never lit for a typed /nucleate (2026-10-23).
        # On "work/tool", which only the web turn reads; the narration is every
        # frontend's. Published first: the web turn narrates the user's line on
        # the start, so the tool's beats follow the turn they belong to.
        name, call_id = "silica_run_injector", f"nucleate-{uuid.uuid4().hex[:8]}"
        args = {"inbox_files": mfs, "target_dir": unit_target}

        def _tell(ev) -> None:
            BUS.publish("work/tool", ev)
            _narr_mod.NARRATOR.on_render_event(ev)

        _tell(ToolStartEvent(name, args, call_id, 0))
        started = _time.monotonic()
        try:
            result, run_id = distill(mfs, unit_target, hub=hub, keep_sources=keep_sources,
                                     seen=seen, profile=profile)
        except ValueError as exc:
            # A path outside the vault (or any other rejected argument) is
            # user error, not a crash: the batch moves to the next unit.
            _tell(ToolErrorEvent(name, call_id, str(exc), 0))
            console.print(f"  [yellow]nucleate: {exc}[/]")
            return None
        except BaseException as exc:
            _tell(ToolErrorEvent(name, call_id, str(exc) or type(exc).__name__, 0))
            raise
        _tell(ToolCompleteEvent(name, args, call_id,
                                json.dumps(project_result(result, run_id, len(mfs)), default=str),
                                _time.monotonic() - started, 0))
        console.print(
            f"  {_at()}{head}nucleate finished: "
            f"{_nucleate_result_line(result)} — details in log.md"
        )
        dispatched += 1
        return None

    # Conversion is local OCR, distillation is network LLM — different
    # resources, so the NEXT book converts while the current unit distills.
    # One worker, one conversion ahead: further parallel OCR just contends
    # for the same GPU. Worth ~2% of wall-clock, not the 2x this comment
    # used to claim: measured 2026-08-16 on 07-religioni-comparate, OCR is
    # 43-68 s per book against 6+ min of distillation. The 2x ceiling needs
    # a corpus where OCR time approaches LLM time, which text PDFs never do.
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1) if to_convert else None
    leaf_prior: dict[int, str | None] = {}

    def _submit(i: int):
        if pool is None or i >= len(to_convert):
            return None
        if batch:
            console.print(
                f"  {_at()}[{i + 1}/{len(to_convert)}] converting {to_convert[i]}"
            )
        leaf = leaf_for.get(to_convert[i])
        if leaf and undo_run:
            # Read before the conversion writes it: a re-imported paper's
            # leaf must be restored by /revert, not deleted.
            leaf_prior[i] = _leaf_text(leaf[0])
        if leaf:
            return pool.submit(convert, to_convert[i], convert_dest, leaf=leaf)
        return pool.submit(convert, to_convert[i], convert_dest)

    pending = _submit(0)
    try:
        for k, (label, segs) in enumerate(ready_units):
            msg = _dispatch_unit(label, segs, picks.get(k))
            if msg:
                return msg
        for i, src in enumerate(to_convert):
            try:
                segs = pending.result()
            except (ValueError, RuntimeError) as e:
                console.print(f"  [yellow]Skipped {src}: {e}[/]")
                if isinstance(e, GpuOutOfMemory):
                    # The GPU stays full until something else lets go of it, so
                    # every later file fails the same way: 129 in a row at ~8 s
                    # each beside a 9B chat server (2026-10-12). They stay in
                    # the inbox for a run once the GPU is free.
                    left = len(to_convert) - i - 1
                    if left:
                        console.print(f"  [yellow]GPU out of memory: left {left} "
                                      f"file(s) unconverted; free the GPU and "
                                      f"/nucleate again[/]")
                    break
                segs = []
            if i in leaf_prior and segs and undo_run:  # leaf_prior fills only under an undo_run
                from silica.kernel.write import gate
                gate.record_landed(undo_run, gate.undo_of(_leaf_rel(leaf_for[src][0]), leaf_prior[i]))
            pending = _submit(i + 1)
            if segs:
                before = dispatched
                msg = _dispatch_unit(src, segs, picks.get(len(ready_units) + i))
                if msg:
                    return msg
                if src in leaf_for and dispatched > before:
                    _zotero_backlink(leaf_for[src], undo_run, console)
    finally:
        if pool is not None:
            # wait=False: a mineru run cannot be cancelled; on an early
            # agent-fallback return the in-flight conversion finishes in
            # the background and its segments are reused next time.
            pool.shutdown(wait=False)

    if total_units and not dispatched:
        console.print(
            "  [dim]nothing left to distill — everything was filed, "
            "skipped, or already in the vault[/]"
        )
    if batch and dispatched > 1:
        console.print(
            f"  [dim]{dispatched} run(s) — /revert undoes the most recent; "
            f"run it again for the one before[/]"
        )
    return ""
