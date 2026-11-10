# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L1 Kernel: the vault's mechanics, as functions on Driver outputs.

From SILICA.md §3 L1, 'mechanical work must be executed by scripts': graph
structure is built without a model (the P2/DKB import contract). The lanes
that write or judge (distill, dedup, Learn's grading) do call one, through
silica.model below them.
"""
from __future__ import annotations

from silica.kernel.base import vault_seam


def _vault_over(transport):
    """Lazy, because this package runs at the first import of anything under
    it, and the policy pulls every write lane in. A Vault passes through, so
    handing get_driver()'s result back to set_driver wraps nothing twice."""
    from silica.kernel.vault import Vault

    return transport if isinstance(transport, Vault) else Vault(transport)


vault_seam.wrap = _vault_over
