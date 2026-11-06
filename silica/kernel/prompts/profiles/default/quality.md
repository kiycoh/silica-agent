## Content Quality Requirements
- All distilled facts MUST be written in {LANGUAGE}. Match the register to the source: scholarly for academic material, plain factual prose for conversational or personal material. Never inflate the register beyond the source.
- Preserve every formula, equation, code block, and numeric value verbatim from the inbox excerpt.
- Use bold for key terms when natural. Use Obsidian callout syntax (> [!TIP], > [!NOTE], > [!WARNING]) only when it materially aids the reader.
- **Modular Atomicity**: if a single payload concept actually bundles multiple distinct sub-concepts (e.g. an inbox section titled "Reti Neurali" that defines both "Perceptron" and "Backpropagation" with separate formulas), split it into multiple update entries — one per atomic concept — with op `write` for each sub-concept that lacks its own vault note.
- **Content Preservation**: do not silently drop information from inbox_excerpt. If it doesn't fit one concept's update, route it to a separate update.
- **Note Title Elegance**: `title` controls BOTH the filename and the H1 heading rendered inside the note. When set, `title` is what the reader sees. Use it whenever the raw `heading` would produce a poor display title. Two mandatory patterns:
  1. **Structural compound headings** — a broad contextual prefix followed by the actual concept. Strip the prefix entirely; keep only the specific concept:
     - `"II Framework PEAS Actuators"` → `"title": "PEAS Actuators"`
     - `"Introduzione ai Sistemi: MCU"` → `"title": "Microcontrollori (MCU)"`
     - `"Lezione 3 — Apprendimento Supervisionato: SVM"` → `"title": "SVM"`
     - Pattern: anything matching `"(Chapter/Lezione/Framework/Sistema) N? [—:] Concept"` → keep only `Concept`.
  2. **Categorical placeholder headings** — single all-caps words or generic category labels that describe the *section* in the inbox file, not the concept itself. If the inbox excerpt discusses a specific concept but the heading is a generic label (e.g. `"UTENTE"` for a section about user modelling, `"RETI"` for a section about neural networks, `"STRUTTURA"` for a section about data structures), you MUST set `title` to the specific concept actually discussed in `inbox_excerpt`. Derive it from the excerpt content, not from the heading.
     - `"UTENTE"` with excerpt about modelli utente → `"title": "Modello Utente"`
     - `"RETI"` with excerpt about reti neurali ricorrenti → `"title": "Reti Neurali Ricorrenti"`
  The `heading` MUST still match the payload concept name exactly (traceability anchor). The `path` MUST use `title`: `{TARGET}/<title>.md`. Omit `title` entirely when the heading is already a precise, atomic concept name.
