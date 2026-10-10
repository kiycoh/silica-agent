# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The learn engine (docs/specs/learn-suite.md, ADR-0040): one set of pure
functions over the quiz ledger, the notes and the graph, reached by the REPL
through `silica_learn_items` and by the GUI through `learn_routes`.

schedule  — FSRS-6 replay of the ledger (D10)
pick      — the four pools, the interleave rule, the amnesty (D13)
items     — item generation: mechanical kinds, the model contract, grounding (D15)
grade     — the blind grader and the rating caps (D12)
plans     — scope resolution and the plan note (D23, D24)
mastery   — the readings of D25 and the calibration signals of D26
"""
