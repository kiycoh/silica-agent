# Examples

**Source (clipped article with owner commentary):**
```
Clipped 2026-06-20 from a pricing teardown post.

The post argues that hosted inference prices fell 71% between early 2025 and
mid 2026 for the mid-tier models, but that the frontier tier has been flat.

Relevant to the local-first studio video. Also directly contradicts what I
said on camera in ep 142.
```

**Good op (two voices, both kept):**
```json
{
  "op": "write",
  "heading": "inference pricing teardown",
  "title": "Hosted Inference Price Collapse",
  "snippet": "According to the clipped pricing teardown, hosted inference prices fell 71% between early 2025 and mid 2026 for mid-tier models, while the frontier tier stayed flat.\n\nI saved this for the local-first studio video. It also directly contradicts what I said on camera in ep 142."
}
```

**Bad op (owner voice demoted, register inflated):**
```json
{
  "op": "write",
  "heading": "inference pricing teardown",
  "title": "Inference Pricing",
  "snippet": "Inference pricing is the cost structure applied to hosted model APIs. Prices for mid-tier models have declined substantially.\n\nRelated: [[ep-142]]"
}
```
The bad op defines a term the source never defined, turns the owner into
nobody, and demotes the commentary to a link. Every one of those is
distill-loss.
