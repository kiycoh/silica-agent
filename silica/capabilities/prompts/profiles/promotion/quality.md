## Content Quality Requirements (EXTRACTIVE — enforced)
The body is built by COPYING fact lines from the excerpt, not by writing about
them. A mechanical validator checks every body line against the source and
REJECTS the op (costing the attempt) if any line is not a verbatim span.
Follow these or the op is rejected:

- **Copy, never reword.** Each body line MUST be an exact substring of the
  concept's `inbox_excerpt` — the same words, in the same order. Do not
  paraphrase, condense, translate, or "clean up".
- **Keep the dated history.** The `- [since YYYY-MM-DD]` prefixes and the
  `(previously: ...)` supersede lines ARE the content — a promoted memory is
  valuable precisely because it is dated. Copy them verbatim.
- **Key markers are allowed.** You may keep the stub's `**<key>**` bold
  lines between copied fact lines (they are verbatim spans of the stub);
  everything else must be a copied span too.
- **No added prose.** No connective sentences, no summaries, no "This note
  documents ...". A descriptive body is distill-loss and is worse than no note.
- **Body INLINE in `snippet`.** As in every profile, the body goes INSIDE
  the JSON as the `snippet` string with `\n` line breaks. Promotion bodies
  are plain dated fact lines — no LaTeX or code, so no backslash escaping
  to worry about.
- **No wikilinks in the body.** The autolink phase adds links mechanically
  after the write; inserted `[[...]]` would corrupt the verbatim span.
- **Note Title Elegance**: `title` controls the filename and H1; derive it
  from the entity in the user's language ("Rex, il cane", "Sam's job") —
  never the raw dotted key. The `heading` MUST still equal the payload concept
  name exactly (traceability anchor). The `path` MUST be `{TARGET}/<title>.md`.
  The title is the ONLY field you author freely — the body is copied.
