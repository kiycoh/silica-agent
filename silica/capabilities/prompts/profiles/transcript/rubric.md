## Decision Rubric
The source is a conversational transcript (chat log, interview, therapy or
coaching session, meeting). Concepts anchor stretches of dialogue; excerpts are
dialogue windows, not expository sections. The anchor name may be weak (a
person, a topic word) — the value is in the facts stated inside the window.
For every concept in every batch, decide exactly ONE action:

- **patch** — vault_collision is not null AND the dialogue excerpt contains durable facts (events, decisions, biographical details, dates, named entities, stated preferences, commitments) that are NOT present in vault_collision.excerpt. Extract only the missing facts. When `graph_context.is_hub` is true, the matched note is a structural anchor of its cluster — prefer `patch` even at lower confidence rather than creating a shadow note.
- **write** — vault_collision is null AND the excerpt carries durable facts that stand on their own. The note collects the facts about the concept's subject (a person, an event, a recurring theme), each attributed to the speaker who stated it.
- **skip** — the excerpt is conversational mechanics (greetings, filler, scheduling chatter, "talk soon"), OR vault_collision.excerpt already covers everything durable, OR the excerpt's only content is time-bound personal facts — those belong in `ephemerals`, and a skip op still emits its ephemerals.
- Every write/patch op MUST set `"linked_axis"` to exactly one of `main_thematic_axes`. If a concept does not substantively expand any axis (a passing mention, a pleasantry), emit `"op": "skip"` with `"reason": "off-axis"`.

The `action_hint` field is the Router's mechanical guess based on collision tiers. Treat it as a starting bias, NOT a binding constraint. You may overrule it based on the actual excerpt content — e.g. an `enrich`-hinted concept where the vault excerpt already contains all the durable facts becomes a `skip`.
