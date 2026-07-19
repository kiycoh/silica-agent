## Few-Shot Example

### Example Input Payload:
{
  "schema_version": 1,
  "batches": [
    {
      "inbox_file": "/abs/path/to/inbox/Lecture 04.md",
      "concepts": [
        {
          "name": "Backpropagation",
          "action_hint": "enrich",
          "inbox_excerpt": "Backpropagation computes the gradient of the loss function. Formula: dL/dw = delta * a.",
          "vault_collision": {
            "path": "/abs/path/to/existing/Backpropagation.md",
            "match_type": "title",
            "total_hits": 1,
            "excerpt": "Backpropagation is used in neural networks."
          }
        },
        {
          "name": "Optimization Algorithms: Adam Optimizer",
          "action_hint": "create",
          "inbox_excerpt": "Adam is an optimization algorithm. Parameters: beta1=0.9, beta2=0.999.",
          "vault_collision": null
        },
        {
          "name": "PIL",
          "action_hint": "likely_skip",
          "inbox_excerpt": "Use PIL to load images.",
          "vault_collision": {
            "path": "/abs/path/to/existing/PIL.md",
            "match_type": "title",
            "total_hits": 1,
            "excerpt": "PIL is Python Imaging Library."
          }
        }
      ]
    }
  ]
}

### Example Output (NO prose, NO markdown fences — JSON, then the Body Appendix):
{
  "main_thematic_axes": ["gradient-based optimization", "neural network training", "Python ML tooling"],
  "updates": [
    {
      "heading": "Backpropagation",
      "op": "patch",
      "path": "/abs/path/to/existing/Backpropagation.md",
      "source_basename": "Lecture 04.md",
      "linked_axis": "neural network training",
      "concepts": ["backpropagation", "loss gradient", "chain rule", "gradient descent"],
      "snippet_ref": 1
    },
    {
      "heading": "Optimization Algorithms: Adam Optimizer",
      "title": "Adam Optimizer",
      "op": "write",
      "path": "{TARGET}/Adam Optimizer.md",
      "source_basename": "Lecture 04.md",
      "hub": "{HUB_NAME}",
      "linked_axis": "gradient-based optimization",
      "concepts": ["adam optimizer", "stochastic optimization", "adaptive momentum", "RMSProp", "AdaGrad"],
      "snippet_ref": 2
    },
    {
      "heading": "PIL",
      "op": "skip",
      "source_basename": "Lecture 04.md",
      "reason": "off-axis — acronym mentioned in passing, no substantive content beyond what's already documented in the vault"
    }
  ]
}
===SILICA-BODY 1===
- Formal definition: backpropagation computes the gradient of the loss function with respect to the weights via the chain rule, applied recursively from the output layer back to the input.
- Gradient formula for weight $w_{ij}^{(l)}$: $\frac{\partial \mathcal{L}}{\partial w_{ij}^{(l)}} = \delta_j^{(l)} \cdot a_i^{(l-1)}$
===SILICA-BODY 2===
Adam (Adaptive Moment Estimation) is a stochastic optimization algorithm that combines the advantages of AdaGrad and RMSProp by maintaining moving averages of both the gradient (first moment) and its square (second moment).

- Typical hyperparameters: $\beta_1 = 0.9$, $\beta_2 = 0.999$, $\epsilon = 10^{-8}$

### Example with `parent` field (only when ## Related Notes lists a valid parent candidate):
Suppose `## Related Notes (candidates)` contains:
- [[Neural Networks]] (score=0.912) [graph-far]

And the payload has concept "Backpropagation" with no vault collision.
The distiller MAY emit:
{
  "heading": "Backpropagation",
  "op": "write",
  "path": "{TARGET}/Backpropagation.md",
  "source_basename": "Lecture 04.md",
  "hub": "{HUB_NAME}",
  "parent": "Neural Networks",
  "linked_axis": "neural network training",
  "snippet_ref": 1
}
(with the body in `===SILICA-BODY 1===` as usual). `parent` MUST be a bare title from the candidates list. Omit it entirely if no candidate is a meaningful parent — the system falls back to hub automatically.