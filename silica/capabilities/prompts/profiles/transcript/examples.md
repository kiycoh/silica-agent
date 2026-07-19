## Few-Shot Example

### Example Input Payload:
{
  "schema_version": 1,
  "batches": [
    {
      "inbox_file": "/abs/path/to/inbox/session_2026-05-07.md",
      "concepts": [
        {
          "name": "pottery class",
          "action_hint": "create",
          "inbox_excerpt": "Elena: I finally signed up for the pottery class at the community center! It starts May 20th, every Tuesday evening.\nSam: That's great! Is that the one your sister teaches?\nElena: No, she teaches the advanced one. Mine is the beginners course with Mr. Alvarez.",
          "vault_collision": null
        },
        {
          "name": "greetings",
          "action_hint": "likely_skip",
          "inbox_excerpt": "Sam: Hey Elena! How have you been?\nElena: Good, good. Busy week!",
          "vault_collision": null
        }
      ]
    }
  ]
}

### Example Output (NO prose, NO markdown fences — JSON, then the Body Appendix):
{
  "main_thematic_axes": ["Elena's hobbies and classes", "family relationships", "conversational logistics"],
  "updates": [
    {
      "heading": "pottery class",
      "title": "Elena's pottery class",
      "op": "write",
      "path": "{TARGET}/Elena's pottery class.md",
      "source_basename": "session_2026-05-07.md",
      "hub": "{HUB_NAME}",
      "linked_axis": "Elena's hobbies and classes",
      "concepts": ["pottery class", "community center", "beginners course"],
      "snippet_ref": 1
    },
    {
      "heading": "greetings",
      "op": "skip",
      "source_basename": "session_2026-05-07.md",
      "reason": "conversational mechanics — no durable facts"
    }
  ],
  "ephemerals": [
    {"key": "elena.pottery_class.start_date", "text": "Elena's pottery class starts on 2026-05-20 (\"May 20th\"), Tuesday evenings"},
    {"key": "elena.pottery_class.teacher", "text": "Elena's beginners pottery class is taught by Mr. Alvarez"}
  ]
}
===SILICA-BODY 1===
- Elena signed up for the beginners pottery class at the community center, starting 2026-05-20 ("May 20th"), held every Tuesday evening.
- The class is taught by Mr. Alvarez. Elena's sister teaches the advanced course at the same center, not Elena's.

Note what the example does NOT do: the body never says "Elena and Sam discussed a pottery class" — it carries the facts themselves, attributed. Time-bound personal details also went to `ephemerals` under stable `entity.attribute` keys.

### Example with `parent` field (only when ## Related Notes lists a valid parent candidate):
Suppose `## Related Notes (candidates)` contains:
- [[Elena]] (score=0.904) [graph-far]

Then the "pottery class" write op MAY add `"parent": "Elena"` — the person note is a genuine conceptual parent of a note about her class. `parent` MUST be a bare title from the candidates list. Omit it entirely if no candidate is a meaningful parent — the system falls back to hub automatically.
