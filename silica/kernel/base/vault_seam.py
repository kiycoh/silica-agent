# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where the driver finds what wraps a transport.

The driver sits below the kernel lanes and the write policy above them, so the
one cannot import the other and a lazy import would only hide that it did.
`silica.kernel` sets `wrap` when it is imported, and Python runs that package
before it hands out this module, so whoever can read `wrap` finds it set.
"""
from __future__ import annotations

from typing import Any, Callable

wrap: Callable[[Any], Any]
