## Few-Shot Example

### Example Input Payload:
{
  "schema_version": 1,
  "batches": [
    {
      "inbox_file": "/abs/path/to/Inbox/user.dog.md",
      "concepts": [
        {
          "name": "user.dog.name",
          "action_hint": "create",
          "inbox_excerpt": "# user.dog\n\n**user.dog.name**\n- [since 2026-06-11] Rex\n\n**user.dog.breed**\n- [since 2026-06-12] German shepherd\n  (previously: unknown breed, 2026-06-11 to 2026-06-12)",
          "vault_collision": null
        },
        {
          "name": "german shepherd",
          "action_hint": "create",
          "inbox_excerpt": "**user.dog.breed**\n- [since 2026-06-12] German shepherd\n  (previously: unknown breed, 2026-06-11 to 2026-06-12)",
          "vault_collision": null
        }
      ]
    }
  ]
}

### Example Output (NO prose, NO markdown fences — JSON only, body inline):
{
  "main_thematic_axes": ["the user's dog", "user context"],
  "updates": [
    {
      "heading": "user.dog.name",
      "title": "Rex (the user's dog)",
      "op": "write",
      "path": "{TARGET}/Rex (the user's dog).md",
      "source_basename": "user.dog.md",
      "hub": "{HUB_NAME}",
      "linked_axis": "the user's dog",
      "concepts": ["user.dog.name", "user.dog.breed"],
      "snippet": "**user.dog.name**\n- [since 2026-06-11] Rex\n\n**user.dog.breed**\n- [since 2026-06-12] German shepherd\n  (previously: unknown breed, 2026-06-11 to 2026-06-12)"
    },
    {
      "heading": "german shepherd",
      "op": "skip",
      "source_basename": "user.dog.md",
      "reason": "merged into the entity note"
    }
  ],
  "ephemerals": []
}
Note what this output IS and IS NOT:
- ONE note for the whole entity: both attributes, copied verbatim into
  `snippet`, dated history included. The second concept is a skip
  ("merged"), not a second note.
- Body INLINE: the body goes INSIDE the JSON as the `snippet` string (with
  `\n` line breaks), as in every profile. Promotion bodies are plain dated
  fact lines, never LaTeX or code. Do NOT emit `snippet_ref` or
  `===SILICA-BODY===` blocks.
- `ephemerals` is empty: the facts came FROM episodic memory. Nothing was
  paraphrased; only the title was authored.
