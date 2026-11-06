## Decision Rubric
For every concept in every batch, decide exactly ONE action:

- **patch** — vault_collision is not null AND inbox_excerpt contains facts (definitions, formulas, examples, code blocks, structural notes, tables) that are NOT present in vault_collision.excerpt. Extract only the missing facts. When `graph_context.is_hub` is true, the matched note is a structural anchor of its cluster — prefer `patch` even at lower confidence rather than creating a shadow note.
- **write** — vault_collision is null (concept is new to the vault). Extract a complete, factually-dense definition plus supporting details for a new spoke note.
- **skip** — vault_collision is not null AND already covers everything in inbox_excerpt, OR the concept is genuine semantic noise (generic acronym, slide marker, rhetorical fragment) with no substantive payload.
- Every write/patch op MUST set `"linked_axis"` to exactly one of `main_thematic_axes`. If a concept does not substantively expand any axis (mere mention, unexpanded acronym, passive bibliographic reference), emit `"op": "skip"` with `"reason": "off-axis"`.

The `action_hint` field is the Router's mechanical guess based on collision tiers. Treat it as a starting bias, NOT a binding constraint. You may overrule it based on the actual excerpt content — e.g. an `enrich`-hinted concept where the vault excerpt already contains all the inbox facts becomes a `skip`.
