# Content Quality Requirements (CLIP — two voices, separated by construction)
- All distilled facts MUST be written in {LANGUAGE}. Never inflate the register
  beyond the source.
- **Two voices, never merged.** Clipped source material is attributed to its
  source (site, author, or page title, as the file's frontmatter or `Source:`
  header names it). The vault owner's own commentary around the clip is
  rendered in first person ("I", never "the creator" or "the user") as stated
  fact in the note body.
- **Owner commentary is body prose, never a link.** A sentence like "this
  contradicts what I said in episode 142" MUST survive as a sentence in the
  body. Demoting it to a wikilink or a `related:` entry is distill-loss: the
  claim disappears and only an edge remains. The autolink phase adds links
  mechanically after the write; do not substitute one for the claim.
- **Extract, lean verbatim.** Prefer copying the source's own wording for
  claims, and preserve numbers, names, dates, percentages, and quoted phrases
  exactly as written. Do not round, translate, or paraphrase a figure.
- **No encyclopedia openers.** Never open a note by defining a term the source
  did not define ("X is a ..."). The note carries what the clip claims, not a
  primer on its topic.
- **Modular Atomicity**: if one concept bundles several distinct claims, split
  it into multiple update entries, one per claim cluster that stands alone.
- **Content Preservation**: do not silently drop claims or the owner's
  commentary from inbox_excerpt. If something does not fit one concept's
  update, route it to a separate update.
- **Note Title Elegance**: `title` controls BOTH the filename and the H1. Name
  the claim or the subject, never a sentence fragment. The `heading` MUST
  still match the payload concept name exactly (traceability anchor); the
  `path` MUST use `title`: `{TARGET}/<title>.md`.
