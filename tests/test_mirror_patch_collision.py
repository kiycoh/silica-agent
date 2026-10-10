# SPDX-License-Identifier: AGPL-3.0-or-later

"""Safe mode judges a patch in vault space and lands it in mirror space.

The model reads TARGET (`silica/...`) and hands the collision back
mirror-prefixed. The boundary check already exempts patches for exactly this
reason (validate.py, `mirror_patch`), but the expected-collision comparison
still assumed a vault-space path and rejected the op ~25 lines before the
rebase that reconciles the two. Measured on a real vault: 22 of 32
collision-path rejections were the mirror prefix and nothing else.
"""
from __future__ import annotations

import pytest

from silica.kernel.vault_manifest import reset_manifest_cache

_COLLISION = "Matematica/Statistica/Correlazione lineare.md"
_BODY = "corpo dell'arricchimento " * 12


@pytest.fixture
def mirror_vault(tmp_vault, monkeypatch):
    """A vault in safe mode: `silica/` mirrors the vault's own tree."""
    from pathlib import Path

    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    (Path(CONFIG.vault_path) / "vault.yaml").write_text(
        "write_dir: silica\n", encoding="utf-8"
    )
    reset_manifest_cache()
    return tmp_vault


def _payload(collision: str, heading: str = "Correlazione"):
    return [{
        "schema_version": 1,
        "batches": [{
            "inbox_file": "Inbox/Lezione 9.md",
            "concepts": [{
                "name": heading,
                "action_hint": "enrich",
                "inbox_excerpt": _BODY,
                "vault_collision": {"path": collision, "excerpt": _BODY},
            }],
        }],
    }]


def _patch(path: str, heading: str = "Correlazione"):
    return {"op": "patch", "heading": heading, "source_basename": "Lezione 9.md",
            "path": path, "snippet": _BODY}


def _validate(ops, payloads, target_dir="silica/Informatica"):
    from silica.kernel.write.validate import validate_operations

    return validate_operations(ops, payloads, target_dir)


def _patched(validated):
    """Paths of the patch ops only — validate also emits the target's hub note."""
    from silica.kernel.write.ops import OpType

    return [o.path for o in validated if o.op == OpType.patch]


def test_a_mirror_prefixed_collision_enriches_the_original(mirror_vault):
    """`silica/<collision>` names the note the payload named — judge it there."""
    mirror_vault.note(_COLLISION, "# Correlazione lineare\n")

    validated, rejected = _validate(
        [_patch("silica/" + _COLLISION)], _payload(_COLLISION)
    )

    assert rejected == []
    # Landing stays in the mirror: the vault note is untouched until pasted.
    assert _patched(validated) == ["silica/" + _COLLISION]


def test_the_vault_space_path_still_works(mirror_vault):
    """The contract the prompt states, unchanged — this always passed."""
    mirror_vault.note(_COLLISION, "# Correlazione lineare\n")

    validated, rejected = _validate([_patch(_COLLISION)], _payload(_COLLISION))

    assert rejected == []
    assert _patched(validated) == ["silica/" + _COLLISION]


def test_a_different_note_is_still_rejected(mirror_vault):
    """The repair is the prefix and nothing else: a wrong note stays wrong."""
    mirror_vault.note(_COLLISION, "# Correlazione lineare\n")
    mirror_vault.note("Matematica/Statistica/Altro.md", "# altro\n")

    validated, rejected = _validate(
        [_patch("silica/Matematica/Statistica/Altro.md")], _payload(_COLLISION)
    )

    assert validated == []
    assert "does not match expected collision" in rejected[0].reason


def test_the_repair_is_counted(mirror_vault):
    """normalized_out names the prompt rule to change, like every other repair."""
    mirror_vault.note(_COLLISION, "# Correlazione lineare\n")
    from silica.kernel.write.validate import validate_operations

    counts: dict = {}
    validated, rejected = validate_operations(
        [_patch("silica/" + _COLLISION)], _payload(_COLLISION),
        "silica/Informatica", normalized_out=counts,
    )

    assert rejected == []
    assert counts.get("mirror_patch_unrebase") == 1


def test_outside_mirror_mode_the_prefix_is_not_repaired(tmp_vault, monkeypatch):
    """`docs/silica` is Silica's own folder in a repo, not a mirror of it."""
    from pathlib import Path

    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    (Path(CONFIG.vault_path) / "vault.yaml").write_text(
        "write_dir: docs/silica\n", encoding="utf-8"
    )
    reset_manifest_cache()
    tmp_vault.note(_COLLISION, "# Correlazione lineare\n")

    validated, rejected = _validate(
        [_patch("docs/silica/" + _COLLISION)], _payload(_COLLISION),
        target_dir="docs/silica/Informatica",
    )

    assert validated == []
    assert rejected != []
