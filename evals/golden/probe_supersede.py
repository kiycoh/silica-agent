# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""probe_supersede — reliability-conditioned resolution (spec-contested-bitemporal §8.2).

Two arms over the vault's own notes, zero-API and deterministic.

Wrong arm (gated on RISE):
    A resolution is WRONG when the claim that survives ranks BELOW the claim it
    absorbed. Both the pairs and the verdict come from the production functions
    (``runners._scan_dedup_pairs`` then ``_pairs_to_items``), so the probe
    measures the seam and cannot drift from the rule it guards.

    Measured on the golden vault (796 notes, 1064 pairs, 54 of them tier-split):
    0 inversions under `merge_rank` = (tier, len), and **43** under the bare
    `len(body)` it replaced — rate 0.0000 against 0.0209. So the gate does catch
    a revert of §6.2, though only just: 2.09pp against a 2pp tolerance. A
    partial revert would slip under it. Tighten the tolerance for this key, or
    gate `wrong_merges` as a count, if that margin ever matters.

Missed arm (informational until a baseline records it):
    A contest that a strict tier dominance would settle, but which sits open.
    This is the size of the §6.1 lever, measured before deciding to build it.
    Only claim-against-source contests are ranked (a `flagged:` ref is a user's
    doubt, with no second claim to weigh), and the incoming side is ranked by
    §5's rule rather than by reading the leaf as a note — see `_incoming_tier`.
    The direction is reported split: only `missed_target_wins` is reachable by
    the §6.1-bis variant, so the two counts price the two designs separately.

Both rates are omitted when their denominator is zero. A rate over nothing
reads as "clean" in the table and freezes into the baseline as a guarantee that
nothing was measured.
"""
from __future__ import annotations

from pathlib import Path


def _incoming_tier(vault, ref: str) -> int | None:
    """The tier of a contested claim that is not a note: None when unrankable.

    NOT `reliability_tier` of the leaf. A leaf carries `date`/`source_id` and no
    `AI` key, so ranking it returns TIER_HUMAN — "the agent did not write this",
    which is true of a raw document and says nothing about whether a person
    vouched for the claim. §5's own definition applies instead: an incoming
    claim is grounded while its verbatim source is reachable, distilled once it
    is not. A `flagged:` ref has no source side at all and is not a claim-vs-
    claim contest, so it is not ranked.
    """
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.write.contested import TIER_DISTILLED, TIER_GROUNDED, ref_source

    basename = ref_source(ref)
    if not basename:
        return None
    leaf = vault / SOURCES_DIR / basename
    return TIER_GROUNDED if leaf.is_file() else TIER_DISTILLED


def is_inversion(*, winner: str, loser: str) -> bool:
    """The wrong arm's numerator: the surviving claim ranks below the absorbed one.

    A predicate rather than an inline comparison so it can be tested where it
    actually fires. Through the seam it never does, and a rate that cannot
    count is not a gate — it is a constant with a metric's name.
    """
    from silica.kernel.write.contested import reliability_tier

    return reliability_tier(loser) > reliability_tier(winner)


def merge_verdicts(pairs: list[dict], *, verbose: bool = False) -> dict:
    """Route `pairs` through the real merge seam and rank what it chose.

    `_pairs_to_items` is the production decision (all three note-vs-note seams
    share it), so this cannot drift from the rule it guards. Pairs whose sides
    rank equal are counted in the denominator but can never move the numerator:
    `wrong_tier_split_pairs` is what says whether the gate is armed on this
    corpus at all.
    """
    from silica.driver import DRIVER
    from silica.kernel.write.contested import reliability_tier
    from silica.tools.runners import _pairs_to_items

    evaluated = wrong = split = 0
    examples: list[tuple[str, str]] = []
    for item in _pairs_to_items(pairs):
        winner_path = item.target_path
        loser_path = item.context.get("loser_path")
        if not loser_path:
            continue
        try:
            winner = DRIVER.read_note(winner_path).content or ""
            loser = DRIVER.read_note(loser_path).content or ""
        except Exception:
            continue
        evaluated += 1
        split += reliability_tier(winner) != reliability_tier(loser)
        if is_inversion(winner=winner, loser=loser):
            wrong += 1
            examples.append((winner_path, loser_path))

    out: dict[str, float] = {
        "wrong_pairs_evaluated": evaluated,
        "wrong_tier_split_pairs": split,
        "wrong_merges": wrong,
    }
    # No tier-split pair, no rate. An empty denominator is the obvious way a
    # rate lies; an unreachable numerator is the quiet one. On the 796-note
    # vault this arm evaluated 578 pairs and every single one ranked both sides
    # the same, so 0.0 said "no inversions" when it meant "no inversion was
    # possible" — and the table marked it GATE either way.
    if evaluated and split:
        out["wrong_resolution_rate"] = round(wrong / evaluated, 4)
    if verbose:
        print(f"\nsupersede wrong: {wrong} inversions / {evaluated} merge pairs "
              f"({split} tier-split — the gate is inert without them)")
        for w, l in examples[:10]:
            print(f"   INVERSION  {l!r} outranked its merge target {w!r}")
    return out


def _candidate_pairs() -> list[dict]:
    """The pairs /dedup would actually propose, from its own scanner.

    `_scan_dedup_pairs` is the production pair builder for both note-vs-note
    seams: vectors only, no API, whole vault. The first version of this probe
    borrowed probe_dedup's candidate loop instead — top same-domain candidate
    per note — and that models COLLISION, a different seam with a domain scope
    /dedup does not have. On the golden vault it admitted 578 pairs and not one
    of them was tier-split, because the agent-written notes sit together in
    their own folders: the filter was hiding exactly the pairs the gate exists
    to watch. `get_store()` is keyed on the resolved index path, so this reads
    the vault the runner pointed CONFIG at.
    """
    from silica.tools.runners import _scan_dedup_pairs

    pairs, err = _scan_dedup_pairs("")
    return [] if err else pairs


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "contests"


def _rule_verdict(target_tier: int, incoming_tier: int | None) -> str:
    """What strict tier dominance says: who wins, or nobody."""
    if incoming_tier is None:
        return "unranked"
    if target_tier > incoming_tier:
        return "target"
    if incoming_tier > target_tier:
        return "incoming"
    return "neither"


def _incoming_clock(vault, ref: str) -> str | None:
    """The event clock of the contested source: its leaf's `date:`, or None.

    None means "this side shows no date", which is what lets `suppress_contest`
    tell an undated claim from one demonstrably newer than the note.
    """
    from silica.kernel.write import frontmatter
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.write.contested import ref_source

    basename = ref_source(ref)
    if not basename:
        return None
    leaf = Path(vault) / SOURCES_DIR / basename
    if not leaf.is_file():
        return None
    data, _raw, _body = frontmatter.split(leaf.read_text(encoding="utf-8"))
    date = (data or {}).get("date")
    return str(date) if date else None


def score_fixture(vault=None, *, recency_guard: bool = True, verbose: bool = False) -> dict:
    """Score the labeled contest corpus against the rule §6.1-bis would apply.

    The golden vault carries zero contested notes, so the missed arm of `run()`
    has no denominator there and the whole conditioned-verdict lane was being
    argued without a number. This corpus supplies one.

    Each note carries `fixture_expect` (target / incoming / neither / unranked)
    with a `fixture_why`, both decided by reading the claims — NOT by the tier
    formula. That is the whole point: labels derived from the rule under test
    would make every rate a tautology. Two contests are deliberate traps where
    tier dominance and the right answer part company, the sharper being a stale
    human note contradicted by a fresher sourced claim, which is the ordinary
    memory-update case rather than an exotic one.

    `fixture_wrong_rate` is precision on the contests §6.1-bis would suppress;
    `fixture_missed_rate` is what it leaves open of what is settleable. Being a
    hand-built corpus, these price the failure MODES, not their prevalence.

    `recency_guard=False` scores the variant as originally specced (strict tier
    dominance, nothing else) so the two designs stay comparable in one run. The
    decision itself comes from `suppress_contest`, the production predicate —
    scoring a restatement of it here would measure the wrong thing.
    """
    from silica.kernel.write import frontmatter
    from silica.kernel.link.health import iter_notes
    from silica.kernel.write.contested import (
        contested_refs,
        reliability_tier,
        suppress_contest,
    )

    root = Path(vault) if vault is not None else FIXTURE_DIR
    contests = ranked = settleable = suppressions = wrong = missed = 0
    rows: list[tuple[str, str, str, str]] = []

    for path in iter_notes(root):
        if path.parent.name == "sources":
            continue  # leaves are the incoming side, never a contest of their own
        content = path.read_text(encoding="utf-8")
        refs = contested_refs(content)
        if not refs:
            continue
        data, _raw, _body = frontmatter.split(content)
        label = str((data or {}).get("fixture_expect") or "").strip()
        contests += 1
        incoming_tier = _incoming_tier(root, refs[0])
        verdict = _rule_verdict(reliability_tier(content), incoming_tier)
        if verdict == "unranked" or label == "unranked":
            rows.append((path.stem, label, verdict, "excluded"))
            continue
        ranked += 1
        settleable += label in ("target", "incoming")
        acts = (
            suppress_contest(content, incoming_tier=incoming_tier,
                             incoming_clock=_incoming_clock(root, refs[0]))
            if recency_guard else verdict == "target"
        )
        outcome = "agrees"
        if acts:                           # the only direction §6.1-bis acts in
            suppressions += 1
            if label != "target":
                wrong += 1
                outcome = "WRONG suppression"
        elif label in ("target", "incoming"):
            missed += 1
            outcome = "missed"
        rows.append((path.stem, label, verdict, outcome))

    out = {
        "fixture_contests": contests,
        "fixture_ranked": ranked,
        "fixture_settleable": settleable,
        "fixture_suppressions": suppressions,
        "fixture_wrong": wrong,
        "fixture_missed": missed,
    }
    if suppressions:
        out["fixture_wrong_rate"] = round(wrong / suppressions, 4)
    if settleable:
        out["fixture_missed_rate"] = round(missed / settleable, 4)

    if verbose:
        print(f"\nsupersede fixture: {suppressions} suppressions, {wrong} of them wrong; "
              f"{missed} settleable contests left open of {settleable}")
        for stem, label, verdict, outcome in rows:
            print(f"   {outcome:<18} label={label:<9} rule={verdict:<9} {stem}")
    return out


def run(vault, *, embed_store=None, cooccur_store=None, verbose: bool = False) -> dict:
    from silica.kernel.link.health import iter_notes
    from silica.kernel.write.contested import contested_refs, reliability_tier

    contested = 0
    missed_eval = incoming_wins = target_wins = 0
    missed_examples: list[tuple[str, int, int, str]] = []

    for path in iter_notes(vault):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        refs = contested_refs(content)
        if not refs:
            continue
        contested += 1
        target = reliability_tier(content)
        for ref in refs:
            incoming = _incoming_tier(vault, ref)
            if incoming is None:
                continue  # a user flag, not a claim against a source
            missed_eval += 1
            if incoming > target:
                incoming_wins += 1
            elif target > incoming:
                target_wins += 1
            else:
                continue
            missed_examples.append(
                (path.relative_to(vault).as_posix(), target, incoming, ref)
            )

    out: dict[str, float] = {
        "contested_notes": contested,
        "missed_pairs_evaluated": missed_eval,
        "missed_incoming_wins": incoming_wins,
        "missed_target_wins": target_wins,
    }
    if missed_eval:  # a rate over nothing would freeze into the baseline as "clean"
        out["missed_resolution_rate"] = round(
            (incoming_wins + target_wins) / missed_eval, 4
        )

    if verbose:
        print(f"\nsupersede missed: {incoming_wins + target_wins} settleable "
              f"/ {missed_eval} contests ranked ({contested} contested notes)")
        for note, t, i, ref in missed_examples[:10]:
            print(f"   T{t} note vs T{i} incoming  {note}  <- {ref}")

    # The wrong arm needs retrieval to know which notes would be merged at all.
    # Without the embed index its keys are simply absent, and compare() gates
    # only keys present in both documents — the same self-arming dedup.* uses.
    if embed_store is not None and len(embed_store):
        out.update(merge_verdicts(_candidate_pairs(), verbose=verbose))
    return out
