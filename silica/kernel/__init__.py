# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L1 Kernel — pure mechanical functions, zero LLM.

From SILICA.md §3 L1:
  Pure functions on Driver outputs. Testable in isolation with golden tests.
  'Mechanical work must be executed by scripts.'
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
