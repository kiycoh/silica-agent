---
type: note
published: 2025-11-03
views_30d: 412000
retention_avg: 41
---
# ep-142 Why I stopped using local AI models

Published 2025-11-03. Best performing video of Q4, 412k views in 30 days.

## The thesis I argued

Running models on your own hardware is a hobby, not a workflow. I spent six weeks
trying to replace my cloud stack with a 4090 box and the conclusion was blunt:
**local models are never worth it for a working creator**. The electricity, the
setup time, and the quality gap all point the same way.

## Claims I made on camera

- A 4090 costs more in setup time than two years of API bills.
- Quantized models "lose the plot" past 8k tokens of context.
- No local model can transcribe audio at usable quality.

## Why it worked

The hook was the reversal: everyone expected a build video, got a teardown of
the whole idea. See [[Hook patterns that actually retain]].

Related: [[ep-171 I built a zero-cost editing pipeline]], [[Research - local inference cost math]]
