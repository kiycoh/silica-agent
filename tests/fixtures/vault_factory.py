"""Synthetic vault factory — deterministic, idempotent test fixture.

Generates a fixed-topology Obsidian vault for CI-reproducible graph tests.
Satisfies contracts C0.1–C0.5 from the WS0 spec.

Default location: tests/fixtures/synthetic_vault/
Override: SILICA_TEST_VAULT env var
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# NoteSpec — the unit of vault topology
# ---------------------------------------------------------------------------

@dataclass
class NoteSpec:
    path: str                         # vault-relative path (e.g. "Hub/Concetti.md")
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    expected_role: str = "normal"     # orphan, hub, spoke, dup-basename, lean, mono, inbox, …


# ---------------------------------------------------------------------------
# SPEC — source of truth for the synthetic vault topology
# ---------------------------------------------------------------------------

SPEC: list[NoteSpec] = [
    # Hub
    NoteSpec(
        path="Hub/Concetti.md",
        frontmatter={"tags": ["concetti"], "AI": True},
        body=(
            "# Concetti\n\n"
            "Questa nota è il hub centrale.\n\n"
            "- [[Backpropagation]]\n"
            "- [[Gradiente]]\n"
            "- [[Percettrone]]\n"
            "- [[A/Cellula]]\n"
            "- [[B/Cellula]]\n"
        ),
        expected_role="hub",
    ),
    # Spoke — resolved links
    NoteSpec(
        path="Concetti/Backpropagation.md",
        frontmatter={"tags": ["concetti"], "AI": True},
        body=(
            "# Backpropagation\n\n"
            "Algoritmo di ottimizzazione.\n\n"
            "## Relazioni\n\n"
            "- [[Hub/Concetti]]\n"
            "- [[Gradiente]]\n"
        ),
        expected_role="spoke",
    ),
    NoteSpec(
        path="Concetti/Gradiente.md",
        frontmatter={"tags": ["concetti"], "AI": True},
        body=(
            "# Gradiente\n\n"
            "Derivata parziale in uno spazio multidimensionale.\n\n"
            "## Relazioni\n\n"
            "- [[Hub/Concetti]]\n"
        ),
        expected_role="spoke",
    ),
    # Spoke — has 1 unresolved link
    NoteSpec(
        path="Concetti/Percettrone.md",
        frontmatter={"tags": ["concetti"], "AI": True},
        body=(
            "# Percettrone\n\n"
            "Modello di neurone artificiale.\n\n"
            "## Relazioni\n\n"
            "- [[Hub/Concetti]]\n"
            "- [[NotaMancante]]\n"
        ),
        expected_role="spoke-unresolved",
    ),
    # Orphan — no incoming links
    NoteSpec(
        path="Isolata/Orfana.md",
        frontmatter={"tags": ["isolata"]},
        body="# Orfana\n\nQuesta nota non ha backlink.",
        expected_role="orphan",
    ),
    # Duplicate basename #1
    NoteSpec(
        path="A/Cellula.md",
        frontmatter={"tags": ["biologia"], "AI": True},
        body=(
            "# Cellula (A)\n\n"
            "Unità fondamentale della vita.\n\n"
            "- [[Hub/Concetti]]\n"
        ),
        expected_role="dup-basename",
    ),
    # Duplicate basename #2
    NoteSpec(
        path="B/Cellula.md",
        frontmatter={"tags": ["biologia"], "AI": True},
        body=(
            "# Cellula (B)\n\n"
            "Variante nella cartella B.\n\n"
            "- [[Isolata/Orfana]]\n"
        ),
        expected_role="dup-basename",
    ),
    # Lean / empty — triage → enrich
    NoteSpec(
        path="Lean/Vuota.md",
        frontmatter={"tags": ["lean"]},
        body="# Vuota\n\n",
        expected_role="lean-empty",
    ),
    # Lean / stub — < 600 chars body, triage → enrich
    NoteSpec(
        path="Lean/Stub.md",
        frontmatter={"tags": ["lean"]},
        body=(
            "# Stub\n\n"
            "Nota molto breve.\n\n"
            "- [[Hub/Concetti]]\n"
        ),
        expected_role="lean-stub",
    ),
    # Monolite — over-limit, ≥2 H2, triage → decouple
    NoteSpec(
        path="Mono/Monolite.md",
        frontmatter={"tags": ["mono"]},
        body=(
            "# Monolite\n\n"
            + ("Testo molto lungo. " * 60) + "\n\n"
            "## Sezione Uno\n\n"
            + ("Contenuto della prima sezione. " * 40) + "\n\n"
            "## Sezione Due\n\n"
            + ("Contenuto della seconda sezione. " * 40) + "\n\n"
            "## Sezione Tre\n\n"
            + ("Contenuto della terza sezione. " * 40) + "\n"
        ),
        expected_role="mono",
    ),
    # Bad frontmatter — inline CSV tags
    NoteSpec(
        path="BadMeta/TagInline.md",
        frontmatter={"tags": "biologia, cellula, mitosi"},  # string instead of list
        body=(
            "# TagInline\n\n"
            "Nota con frontmatter non conforme (tags come stringa CSV).\n"
        ),
        expected_role="bad-meta",
    ),
    # Inbox — collides with Backpropagation
    NoteSpec(
        path="_inbox/Lezione.md",
        frontmatter={},
        body=(
            "# Lezione su Backpropagation\n\n"
            "Appunti dalla lezione. Argomenti: Backpropagation, gradiente, \n"
            "discesa del gradiente, ottimizzazione.\n"
        ),
        expected_role="inbox-collision",
    ),
    # Inbox — new concept, no collision
    NoteSpec(
        path="_inbox/Nuovo.md",
        frontmatter={},
        body=(
            "# Trasformatori\n\n"
            "Architettura Transformer: attention mechanism, self-attention, \n"
            "multi-head attention, encoder-decoder.\n"
        ),
        expected_role="inbox-new",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_ROOT = Path(__file__).parent / "synthetic_vault"
_MANIFEST_NAME = ".silica_fixture_manifest.json"
_SPEC_VERSION = "1"


def _canonical(path: str) -> str:
    """Vault-relative canonical key: strip .md, normalize slashes, lowercase."""
    p = path.replace("\\", "/").strip("/")
    if p.endswith(".md"):
        p = p[:-3]
    return p.lower()


def _spec_sha256() -> str:
    """SHA-256 of the serialised SPEC (used for idempotency check)."""
    content = json.dumps(
        [
            {
                "path": s.path,
                "frontmatter": s.frontmatter,
                "body": s.body,
                "expected_role": s.expected_role,
            }
            for s in SPEC
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _resolve_root() -> Path:
    """Return the vault root: env override or default within the repo."""
    env = os.environ.get("SILICA_TEST_VAULT")
    if env:
        return Path(env)
    return _DEFAULT_ROOT


def _render_note(spec: NoteSpec) -> str:
    """Render a NoteSpec to its full markdown text."""
    import yaml  # PyYAML — already a dev dependency via uv
    parts = []
    if spec.frontmatter:
        parts.append("---")
        parts.append(yaml.dump(spec.frontmatter, allow_unicode=True, default_flow_style=False).rstrip())
        parts.append("---")
        parts.append("")
    parts.append(spec.body.rstrip())
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_synthetic_vault(root: Path, force: bool = False) -> Path:
    """Create the synthetic vault at *root*.

    - If *root* does not exist or has no manifest, creates it from scratch.
    - If the manifest spec_sha256 matches the current SPEC, does nothing (idempotent).
    - If *force* is True, always regenerates.

    Returns the root Path.
    """
    root = Path(root)
    manifest_path = root / _MANIFEST_NAME
    current_sha = _spec_sha256()

    # Idempotency check
    if not force and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("spec_sha256") == current_sha:
                return root  # nothing to do
        except Exception:
            pass  # corrupt manifest → regenerate

    # Write notes
    root.mkdir(parents=True, exist_ok=True)
    for spec in SPEC:
        note_path = root / spec.path
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(_render_note(spec), encoding="utf-8")

    # Write manifest
    manifest = {
        "spec_version": _SPEC_VERSION,
        "spec_sha256": current_sha,
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "notes": [
            {
                "path": s.path,
                "canonical": _canonical(s.path),
                "expected_role": s.expected_role,
            }
            for s in SPEC
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return root
