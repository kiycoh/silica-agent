#!/usr/bin/env bash
# The four checks of .github/workflows/ci.yml in one command, run from the root
# of any worktree, so a branch is called done on what CI will say: the merges
# between v0.24.0 and v0.25.0 ran pytest alone, and Discover landed with five
# mypy errors CI would have failed on (fixed in f294612).
#
# The suite runs under a memory cap and one at a time across worktrees: two
# uncapped runs reached 23 GB on 2026-10-24 and took the desktop session down.
set -euo pipefail
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/silica-gate.lock"
flock 9
uv run ruff check silica tests scripts evals
uv run lint-imports
uv run mypy silica
systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=0 -- uv run pytest -q
