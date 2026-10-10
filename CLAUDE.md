## Comments carry the why

A comment answers **why this and not the obvious alternative**. It never
restates what the line does: that is scaffolding, it rots the moment the line
changes, and it is the reason comment density is *not* a target. Raising the
ratio by commenting the *what* makes the codebase worse.

This is not a new rule, it is the one already practised: 128 of ~185 files under
`silica/` carry rationale prose. Copy the wording from the best of them:

- `silica/config.py:35` documents a **removal** and what made it unsafe.
- `silica/__init__.py:14` explains why a value is captured *here* and not in the
  obvious place.
- `silica/kernel/write/provenance.py:158` explains why something is named once,
  citing the three call sites that disagreed before.

Three tiers, one home each:

- the **local why** (this branch, this constant, this order) is a comment;
- the **why of an approach** (this design and not the one a reader expects) is
  an ADR under `docs/adr/`, cited from the code by number;
- what a **term** means is `CONTEXT.md`, which stays a glossary and never
  acquires implementation detail.

Two shapes are a missing why by definition. Do not write either without one:

- an exception caught and swallowed, which must say what failure is being
  tolerated and why continuing is correct;
- a `ponytail:` marker that names a shortcut without naming the ceiling that
  would invalidate it, and the upgrade path.

Prefer a **number or a date** to a condition, because only those can be checked
later without rereading the code: "reopen at 5k notes" and "kill by 2026-10-28"
survive a grep in a year, "if it ever gets slow" does not. Measured 2026-08-21:
55 of 106 markers name an upgrade condition and almost all of them are
subjective, so they read as complete while nothing can ever fire them.
