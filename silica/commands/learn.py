# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learn's commands: /quiz, /flashcards, /slides, /memorize, and the prompts they ask the model with."""
from __future__ import annotations

import json
from pathlib import Path

from silica.command_table import Call, Result
from silica.session.words import int_flag, positional, str_flag


def flashcards(c: Call) -> Result:
    """/flashcards <note|folder> [--n=12] [--save=<path>]: question-and-answer
    cards for review outside a session, one checked point each."""
    args, out = c.args, c.out
    from silica.kernel.learn.artifacts import flashcards

    targets = positional(args)
    if not targets:
        return out.refused("/flashcards needs a note or a folder. Usage: /flashcards <note|folder> [--n=12] [--save=<path>]")
    out.answer = flashcards(targets, int_flag(args, "--n=", 12), str_flag(args, "--save").strip())
    return out


def slides(c: Call) -> Result:
    """/slides <note|folder> [--save=<path>]: a presentation of what the notes
    say, in the Markdown Obsidian's Slides plugin presents."""
    args, out = c.args, c.out
    from silica.kernel.learn.artifacts import study_points

    save = str_flag(args, "--save").strip()
    targets = positional(args)
    if not targets:
        return out.refused("/slides needs a note or a folder. Usage: /slides <note|folder> [--save=<path>]")
    titles, points = study_points(targets)
    ground = ("Build the slides from these points, each already checked against its source, in this order "
              "unless the topic's logic asks another:\n" + "\n".join(points) if points else
              "Read the note(s) first (list a folder's notes; a lesson's outline note gives the order).")
    then = ("Then write the deck, unchanged, as the note `" + save + "` with silica_write_note, so Obsidian's "
            "Slides plugin presents it."
            if save else "READ-ONLY: do not create, edit, patch, or move any note.")
    # No example embed in the format line: given one, the local 9B put
    # `![[file.png]]` on every slide of a lecture deck (2026-10-23).
    return out.to_model(f"Make a slide deck from {', '.join(f'[[{t}]]' for t in titles) or ', '.join(targets)}.\n"
        f"{ground}\n"
        f"Format: Markdown slides separated by a line holding only `---`. Slide 1 is the title and one line on "
        f"what the deck covers; then 6 to 10 slides, each a `## ` title and 3 to 5 short bullets in the notes' "
        f"own words, formulas kept as written in $...$; a figure only where a note embeds one, its embed line "
        f"copied exactly from the note, and no figure line otherwise; the last slide is three questions the "
        f"reader should now be able to answer. "
        f"Cite each slide's source note as a [[wikilink]] on its last line.\n"
        f"Answer in chat with the deck in one ```markdown block. {then}")


def quiz(c: Call) -> Result:
    """/quiz [note|folder] [--n=10] [--focus="passage"]: an agent-directed
    active-recall round; --focus narrows it to one passage of the note."""
    args, out = c.args, c.out
    focus = str_flag(args, "--focus").strip()
    # A passage holds a few testable claims, not ten: ten questions on one
    # paragraph would be asked by rewording the same sentence.
    n = int_flag(args, "--n=", 3 if focus else 10)
    targets = positional(args)
    if targets and focus:
        # The drawer's selection bar (ADR-0059): the round stays a round on the
        # NOTE, so the ledger lines still carry its path, only the questions
        # are drawn from the passage.
        source = "from `" + targets[0] + "`, on this passage of it only:\n> " + focus.replace("\n", "\n> ")
        pick = ("Read the note for the context the passage sits in, but ask only what the "
                "passage itself states.")
    elif targets:
        source = "from " + ", ".join(f"`{t}`" for t in targets)
        pick = "Read the note(s) (list a folder's notes first)."
    else:
        # No target: the learner model picks. Half the round re-tests what
        # is decaying, half probes what was never measured — every graded
        # answer feeds the same ledger either way (spec: learner-model).
        source = "from the learner model's review queue"
        pick = (
            "Call silica_review_queue to pick the targets, and read them. Mix both pools: "
            "'due' rows were known once and are decaying — re-test them; 'unexplored' rows "
            "were never measured, and the AI-written ones were never learned at all, so "
            "probe those first. An empty queue means an empty vault: say so."
        )
    return out.to_model(f"Run a {n}-question active-recall quiz {source}.\n"
        f"{pick}\n"
        f"Mix recall, comprehension, and application questions; ask only what the notes "
        f"actually support.\n"
        f"Ask the numbered questions and STOP. Do not reveal the answers in the same "
        f"message: retrieving from memory is what makes the round worth running, and a "
        f"visible answer key destroys it.\n"
        f"When the reader replies, grade each answer, cite each source note as a "
        f"[[wikilink]], then call silica_record_quiz once with one entry per question: "
        f"path, correct, concepts (the 1-3 concepts that question tested), q (the "
        f"question text), and anchor ('#Heading' when one heading clearly holds the "
        f"answer). Grade an unanswered or skipped question as incorrect.\n"
        f"After grading, offer ONE follow-up round drilling what was just missed — run "
        f"it only if the reader accepts.\n"
        f"A wrong answer is the reader's miss, not the note's fault, and needs nothing "
        f"beyond the grade. If grading instead exposes a fault in the note itself (it "
        f"states something wrong, or contradicts another note you read), offer to record "
        f"that with silica_flag_note, and only run it once the reader says so.\n"
        f"READ-ONLY apart from that flag: do not create, edit, patch, or move any note.")


def _kv_flag(args: list[str], flag: str) -> str | None:
    """`--flag value` or `--flag=value`; None when absent, "" for a bare flag."""
    for i, a in enumerate(args):
        if a == flag:
            nxt = args[i + 1] if i + 1 < len(args) else ""
            return "" if nxt.startswith("--") else nxt
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


_LEARN_SESSION_RULES = (
    "For each item, in order:\n"
    "1. Ask ONE item per message: the note title and the question, nothing else: no hint, "
    "no note body, and never the answer or the claim wording. STOP and wait.\n"
    "2. When the reader answers, ask for confidence BEFORE any feedback, with exactly these "
    "labels: 90 / 70 / 50 / 25, or Skip (records null). Never infer it; if the reader stated "
    "one already, do not ask twice.\n"
    "3. Hints only on request, at most two, taken from the item's `hints` list in order. "
    "\"Just tell me\" is honored at once: show the answer, say \"Answer shown; independent "
    "recall was not checked.\", and log told=true (Hard at best).\n"
    "4. Grade blind against the source: read `source.leaf` with silica_read_note and quote "
    "the line at `source.line` (the span) beside the note's claim. Rubric: list what is "
    "missing before what is present; meaning over wording; a why question owes a mechanism "
    "(the what alone is partial); fluency is not evidence; when torn, round down and quote "
    "the criterion; an empty answer is lapsed. Outcome recalled/partial/lapsed maps to rating "
    "good/hard/again; easy only when the answer was complete and precise with no hint and "
    "confidence 70 or more. Caps after the rubric: any hint or told caps at Hard; a correct "
    "answer at confidence 25 caps at Good. Show the proposed rating (Again/Hard/Good/Easy) "
    "and accept the reader's change; log both when they differ. If the note's claim is NOT "
    "supported by the span, say \"Source mismatch\", show both, offer silica_flag_note (run "
    "it only on a yes) and record NOTHING for that item.\n"
    "5. Record with silica_record_quiz, one entry per item: path, correct (the rubric met, "
    "before caps), rating, kind, level, q, anchor, concepts, conf, source {leaf, line}, grader "
    "(\"model\", \"self\" or \"told\"), grader_rating when the reader overrode yours, hints "
    "(rungs used), told, objective and point as the item carries them, session (the id "
    "silica_learn_items returned) and plan when one.\n"
    "6. One momentum line only on a milestone (first success on a note, estimated hold "
    "crossing 7 or 30 days, or doubling); never on Hard or Again, never \"keep it up\". "
    "Label an unsuccessful predict item \"First attempt\", not a lapse. Then offer the next "
    "item and STOP.\n"
    "Plain sentences, no praise padding. Question and feedback in the material's language.\n"
    "READ-ONLY apart from silica_record_quiz and a flag the reader asks for."
)


def _learn_session_prompt(n: int, target: str = "") -> str:
    call = f"silica_learn_items(n={n}" + (f", target={json.dumps(target)}" if target else "") + ")"
    where = f" inside the plan `{target}` at its frontier" if target else " from the whole vault"
    return (
        f"Run one study session of n={n} items{where}.\n"
        f"Call {call} once. Relay its `amnesty` and `short` lines verbatim when present; an "
        f"empty item list means the vault has no grounded material in scope: say what is "
        f"missing and stop.\n" + _LEARN_SESSION_RULES
    )


def _learn_plan_prompt(name: str, kind: str, target: str, restrict: str = "") -> str:
    """The syllabus writer (D17, D23, D24): one plan note through the write gate."""
    props = {"type": "plan", "scope": kind, "target": target, "reach": "relational"}
    if restrict:
        props["restrict"] = restrict
    read = {
        "folder": (
            f"Call silica_review_queue(target={json.dumps(target)}): it returns every note "
            f"under the prefix with its R, already in prerequisite order (topological over the "
            f"vault's directed relation). Trust that order; do not re-derive prerequisites "
            f"from prose."
        ),
        "notes": f"Read each of these notes: {target}.",
        "topic": (
            f"Call silica_concepts(term={json.dumps(target)}) for the notes carrying the term "
            f"and its neighbour concepts; read the MOC that names it when one exists."
        ),
        "steps": (
            f"Read `{target}` (a syllabus, a course program, or the directive as typed). "
            f"Resolve EACH step line to the vault notes that cover it: title first, then "
            f"silica_search_context; a step with no covering note is KEPT and marked "
            f"\"not in the vault\" (the plan's most useful line, never dropped)."
            + (f" Restrict resolution to notes under `{restrict}`." if restrict else "")
            + " Then call silica_review_queue(target=...) on the covered area for the order "
            "and the retention state; it is already in prerequisite order, trust it."
        ),
    }[kind]
    steps_rule = (
        "`## Steps`, one `- [ ]` line per step in that order, each with a because clause: "
        "`- [ ] [[Softmax]] before [[Cross-entropy]]: because the loss is defined on the "
        "softmax output`; a gap reads `- [ ] <step>: not in the vault`. Start at the "
        "frontier: skip steps whose notes still hold (high R) and say so in one line."
        if kind == "steps" else
        "no `## Steps` section (the scope orders itself)."
    )
    return (
        f"Write a study plan named `{name}` over the scope {kind}={json.dumps(target)}"
        + (f" restricted to `{restrict}`" if restrict else "") + ".\n"
        f"1. {read}\n"
        f"2. Write ONE plan note via silica_write_note(path=\"Plans/{name}.md\", "
        f"props={json.dumps(props)}) with body: `# {name}`; one plain sentence naming the "
        f"goal; `## Tree` (children as bullets, leaves as [[wikilinks]], a note under several "
        f"children listed once per child); {steps_rule} End with an empty `## Sessions`. "
        f"Nothing about mastery goes in the note: every reading derives from the ledger.\n"
        f"3. Then STOP and ask whether to begin; `/memorize {name}` continues the plan at its "
        f"frontier.\n"
        f"A `type: syllabus` note from before is read as a steps plan; never rewrite one, "
        f"continue it."
    )


def _learn_teach_prompt(note: str) -> str:
    return (
        f"Teach-back on `{note}` (learn-suite D15, kind teach).\n"
        f"Phase 1, four turns: play a curious student who sees ONLY the note's title and its "
        f"concepts (silica_concepts(note={json.dumps(note)})) and never the body, the source "
        f"or the vault. Ask the reader to explain `{note}` to you; on each reply ask ONE naive "
        f"follow-up about what stayed unclear; STOP after every question and wait. No grading, "
        f"no hints, no corrections during the four turns.\n"
        f"Phase 2, the summary: now read the note and its source leaf with silica_read_note. "
        f"Write a summary that checks the reader's explanation for connected propositions "
        f"against the claims (cause, effect, condition, the relation between parts), never "
        f"paraphrase quality or connective counts; quote the span for anything missed. Propose "
        f"a rating (again/hard/good/easy) and accept the reader's change. Record ONE "
        f"silica_record_quiz entry: path, correct, rating, kind \"teach\", level 3, q, conf "
        f"null, source {{leaf, line}}, grader \"model\", grader_rating when overridden, "
        f"session (a fresh uuid).\n"
        f"Plain sentences, no praise padding. READ-ONLY apart from that record."
    )


def memorize(c: Call) -> Result:
    """/learn v2 (learn-suite D20): a session, a plan to continue, a plan to
    write, a teach-back, or the readings. One door, no new command name."""
    args, out = c.args, c.out
    n = int_flag(args, "--n=", 8)
    status = _kv_flag(args, "--status")
    if status is not None:
        from silica.kernel.learn import mastery

        line = mastery.status_line(status)
        return out.to_model(f"Relay this status line to the reader verbatim, then stop:\n{line}")
    teach = _kv_flag(args, "--teach")
    if teach is not None:
        if not teach:
            return out.refused("/memorize --teach requires a note. Usage: /memorize --teach <note>")
        return out.to_model(_learn_teach_prompt(teach))
    plan = _kv_flag(args, "--plan")
    scope = str_flag(args, "--scope")
    steps = str_flag(args, "--steps")
    if plan is not None:
        if not plan:
            return out.refused("/memorize --plan requires a name. Usage: /memorize --plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]")
        if steps:
            return out.to_model(_learn_plan_prompt(plan, "steps", steps, restrict=scope))
        if not scope:
            return out.refused("/memorize --plan needs --scope=<folder|note,note|topic:term> or --steps=<note>")
        if scope.startswith("topic:"):
            return out.to_model(_learn_plan_prompt(plan, "topic", scope[len("topic:"):]))
        if "," in scope or (not scope.endswith("/") and scope.endswith(".md")):
            return out.to_model(_learn_plan_prompt(plan, "notes", scope))
        return out.to_model(_learn_plan_prompt(plan, "folder", scope))
    targets = [a for a in positional(args) if not a.startswith("--")]
    if not targets:
        return out.to_model(_learn_session_prompt(n))
    target = " ".join(targets)
    # A plan by that name continues at its frontier; anything else is the v1
    # syllabus flow, a steps plan written over that scope (D17). The vault
    # is the registry, so the plan lookup is a frontmatter scan, not a store.
    # New-material targets (learner-model extension 1) plug in HERE: resolve
    # `target` against outside sources before the plan is written.
    try:
        from silica.kernel.learn import plans

        found = plans.find_plan(target)
    except Exception:
        found = None
    if found is not None:
        return out.to_model(_learn_session_prompt(n, target=found["path"]))
    return out.to_model(_learn_plan_prompt(Path(target.rstrip("/")).name or target, "steps", target))
