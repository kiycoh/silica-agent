# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The learn engine (docs/specs/learn-suite.md, ADR-0040): one set of pure
functions over the quiz ledger, the notes and the graph, reached by the REPL
through `silica_learn_items` and by the GUI through `learn_routes`, both over
`session.py`.

schedule  — FSRS-6 replay of the ledger (D10)
pick      — the four pools, the interleave rule, the amnesty (D13)
items     — item generation: mechanical kinds, the model contract, grounding (D15)
grade     — the blind grader and the rating caps (D12)
session   — the live session, its ledger line and its receipt (ADR-0135)
reasons   — why a note is up, in the reader's words (ADR-0081)
plans     — the plan note and what it covers (D23, D24)
reference — a reference note and a plan's step line, read as text (ADR-0131)
sources   — collections, their roles, and a reference's topics against the vault
pace      — the workload, the calendar and the budget of a dated plan
sentence  — the sentence of Create plan read into its fields (ADR-0081)
store     — the vault and Learn's append-only logs
mastery   — the readings of D25 and the calibration signals of D26
"""
