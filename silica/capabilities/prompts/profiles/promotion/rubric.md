## Decision Rubric
The source is a PROMOTION STUB: a dated render of an episodic memory chain that
the user consented to turn into a note (`/promote`). Every fact line in it is
durable BY DEFINITION — it survived the episodic gate, recurred across
sessions, and the user consented to keeping it as a note. Your job is
EXTRACTIVE: you SELECT the fact lines and copy them verbatim into ONE note
body. You are a selector, not a rewriter — never paraphrase, summarize, or
re-typeset the selected text.

The stub's H1 is the ENTITY (e.g. `user.dog`); its `##` sections are the
entity's attributes. Emit exactly ONE write or patch op covering the WHOLE
entity — one note per entity, never one per attribute:

- **write** — vault_collision is null. The body is every fact line of the
  stub, copied verbatim, in stub order. Use the FIRST concept name of the
  batch as `heading`.
- **patch** — vault_collision holds an existing note about this entity. Copy
  verbatim only the fact lines not already present in vault_collision.excerpt.
- **skip** — ONLY when vault_collision.excerpt already contains every fact of
  the stub. NEVER skip because the facts are personal, ephemeral, or
  time-bound: this material already lived in ephemeral memory and the user
  promoted it OUT of there — routing it back undoes their command.
- Additional concepts of the same batch beyond the first: `"op": "skip"` with
  `"reason": "merged into the entity note"`.
- Every write/patch op MUST set `"linked_axis"` to exactly one of
  `main_thematic_axes`.

`ephemerals` MUST be an empty list (`[]`): these facts come FROM the episodic
store — re-emitting them there creates duplicates of themselves.
