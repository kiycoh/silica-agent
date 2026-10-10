# Validator characterization

Baseline: `ecd108213bd7917b6da5c60b6055c7a53c4c661d`, captured 2026-09-19.
Ten batches, 52 input operations, exercised as both dictionaries and `Op`
models by `tests/test_validate_characterization.py` (20 tests).

`cases.json` contains the initial vault files, input operations, payloads,
call options and provenance of each case. `expected.json` freezes the complete
returned models, rejection reasons and all five diagnostic out-parameters.
List order and default model fields are preserved. The harness also checks
that caller-owned inputs and vault file contents are unchanged.

The filesystem backend and existing `tmp_vault` fixture are used. No LLM,
developer vault, network, snapshot plugin or production hook is required.
Profiles and vault boundaries are explicit; gate environment overrides are
cleared and the language setting is pinned to `auto`. Default snippet floors
remain observable, so changing a production default changes this baseline.

| Case | Interaction preserved |
| --- | --- |
| coercion-order | Title repair, aliases, superseded redirect, both dedup passes, write/patch/overwrite body transfer and overwrite base snapshot |
| payload-and-references | Source and heading correction, stacked soft warnings, empty-body rejection versus empty-excerpt omission, malformed-op salvage, inbox/sibling rejection, parents and future links |
| mirror-collisions | Collision verification in original space, landing in mirror space, path repairs and diagnostic paths |
| mirror-folders | One justification covers the batch's folder, including an earlier op; other invented folders reject |
| write-boundary | Rebase new writes, protect existing notes and move destinations, reject inbox and unsupported delete |
| grounding-default | Warn-only invented math and whole-source fallback after archiving |
| grounding-extractive / grounding-promotion | Per-run floor and extractivity, retained warnings but removed scores for rejected ops |
| field-lesson-12 | Three captured model operations with real concept excerpts and a dangling reference |
| malformed-ops | Missing heading, missing and unknown source basename with two sources in the chunk, overwrite outside the target folder; an op with an empty path leaves the batch without a verdict |

Eight reduced cases cite existing incident tests in their `origin` fields;
`malformed-ops` (added 2026-09-19) cites the coverage report that showed its
rejection reasons unreached by any test.
The field case retains all three input ops from
`~/.silica/tmp/0e6eaec560a74a07b4cb7148e7793bf6.json`; the matching payload
`31c7a92597fa498eb20dfadc1c848ef8.json` is reduced to those three concepts.
Its empty vault is explicitly reconstructed. The historical pre-write vault
and full call context were not captured together, so this is **not an exact
historical replay** and does not assert the original run's verdicts.
The test uses only the committed copies, never those local paths.

These are observations, not new correctness requirements. For example, a
missing overwrite target degrades to write without entering the write-body
gate; mirror diagnostics precede the final rebase; an extractive rejection
removes its grounding score but keeps its ungrounded warning; an op with an
empty `path` is salvaged to `skip` by `parse_ops` before any gate runs and
is neither accepted nor rejected; the validator's own `Missing 'path'`
guards, a dead second copy of that rule, were deleted on 2026-09-19 once
`parse_ops` re-validated models too. A decomposition
must preserve these behaviors. Any intentional correction needs a separate
change and review of the affected expectation.

Run `uv run pytest tests/test_validate_characterization.py -q`.
On mismatch the test saves the complete observation to `observed.json` under
its pytest temporary directory and prints the path. It never overwrites the
oracle. The initial baseline was recorded from unchanged production code,
checked for dictionary/model equality, and reviewed before freezing it.
Do not regenerate expectations to make a structural refactor pass.

This corpus complements the existing focused tests; it does not establish
exhaustive branch coverage, WS-driver equivalence or full-pipeline behavior.
For an exact future field replay, retain the pre-validation ops, payloads,
initial vault snapshot, call options and diagnostics together for one run.
