import json
from typing import Any
from silica.kernel.ops import Op

def parse_ops(raw: list | dict | Any) -> list[Op]:
    """Parse list or updates dict into a list of Op models."""
    if isinstance(raw, dict) and "updates" in raw:
        items = raw["updates"]
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]
        
    ops = []
    for item in items:
        if isinstance(item, Op):
            ops.append(item)
        else:
            ops.append(Op.model_validate(item))
    return ops

def load_ops(path: str) -> list[Op]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return parse_ops(data)

def dump_ops(path: str, ops: list[Op]) -> None:
    data = [op.model_dump() for op in ops]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
