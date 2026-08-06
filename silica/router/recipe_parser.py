# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

import yaml
from pathlib import Path
from typing import Any, Dict


def load_recipe(recipe_name: str = "injector") -> Dict[str, Any]:
    """Load a bundled recipe (silica/recipes/<name>.yaml)."""
    recipe_path = Path(__file__).resolve().parent.parent / "recipes" / f"{recipe_name}.yaml"
    if not recipe_path.exists():
        raise FileNotFoundError(f"Recipe file not found: {recipe_path}")
    with open(recipe_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
