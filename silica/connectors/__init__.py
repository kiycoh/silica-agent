# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Connectors: Silica reaching an account of the owner beyond the vault.

Settings › Connectors is one section and each connector is one block in it
(ADR-0089 point 8). The blocks are a static tuple, a list and not a plugin
system, as the source registry is (ADR-0014): each provides `status()` (name,
a state word, the account it is bound to, one line of detail, the actions it
accepts) and `act(action, payload)`. `GET /connectors` lists them and
`POST /connectors/<name>/<action>` runs one.

Only stdlib at import time: the server imports this module whatever extras
are installed.
"""
from __future__ import annotations

from typing import Any, Protocol


class Connector(Protocol):
    name: str

    def status(self) -> dict[str, Any]: ...

    def act(self, action: str, payload: dict[str, Any]) -> dict[str, Any]: ...


def connectors() -> tuple[Connector, ...]:
    """The static tuple. A function and not a module constant so a block's
    module is imported only when the panel asks, not by every import of this
    package; ADR-0090's mail block joins it here."""
    from silica.connectors.mcp_block import MCP_BLOCK
    from silica.sources.mail_connector import MAIL_BLOCK

    return (MCP_BLOCK, MAIL_BLOCK)


def find(name: str) -> Connector | None:
    return next((c for c in connectors() if c.name == name), None)
