"""A real SilicaConfig for a test that hands a config around.

A stub carries only the attributes its author thought of, so it hid every read of
one it lacked behind `getattr(config, "x", default)`, and the test ran on that default
instead of the shipped value. This one has every field and refuses a name that is not one.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from silica.config import SilicaConfig


def make_config(**overrides) -> SilicaConfig:
    """The defaults, whatever the shell exported, then `overrides`. Assigned after the
    build because `provider` is a property over the `_provider` field."""
    with patch.dict(os.environ, clear=True):
        config = SilicaConfig()
    for name, value in overrides.items():
        assert hasattr(config, name), f"SilicaConfig has no {name}"
        setattr(config, name, value)
    return config
