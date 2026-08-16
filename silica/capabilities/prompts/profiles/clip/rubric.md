# Decision Rubric
The source is CLIPPED external content: an article, a web page, or a fetched
transcript that the vault owner saved, possibly wrapping it in their own
commentary ("relevant to X", "contradicts what I said in Y"). There are two
voices in the file and they must never merge.

For every concept in every batch, decide exactly ONE action:

- **patch** — vault_collision is not null AND the excerpt carries claims,
  numbers, or arguments not present in vault_collision.excerpt. Extract only
  the missing material. When `graph_context.is_hub` is true, prefer `patch`
  even at lower confidence rather than creating a shadow note.
- **write** — vault_collision is null AND the excerpt carries source claims or
  owner commentary that stand on their own.
- **skip** — the excerpt is boilerplate (navigation, cookie text, bylines
  without content), OR vault_collision.excerpt already covers everything.
- Every write/patch op MUST set `"linked_axis"` to exactly one of
  `main_thematic_axes`; a concept expanding no axis is `"op": "skip"` with
  `"reason": "off-axis"`.

The `action_hint` field is the Router's mechanical guess based on collision
tiers. Treat it as a starting bias, NOT a binding constraint.
