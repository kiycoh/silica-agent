from silica.kernel.templates import ensure_ai_flag, template_spoke


def test_spoke_does_not_double_wrap_bracketed_parent_and_related():
    """Distiller often emits parent/related already as [[X]] (hence write.py's
    .strip('[]')). template_spoke must not re-wrap them into [[[[X]]]], which
    Obsidian reads as an unresolved link and trips the graph regression gate."""
    out = template_spoke(
        heading="Modelli Linguistici Generativi (GPT)",
        snippet="testo",
        hub="[[IA Generativa]]",
        related=["[[Reti Neurali Profonde (Deep Learning)]]", "calcolo parallelo"],
        parent="[[Rinascita dell'IA]]",
    )
    assert "[[[[" not in out and "]]]]" not in out, out
    assert 'parent note: "[[Rinascita dell\'IA]]"' in out
    assert '"[[Reti Neurali Profonde (Deep Learning)]]"' in out
    assert '"[[calcolo parallelo]]"' in out  # bare name still wrapped exactly once
    assert '"[[IA Generativa]]"' in out


def test_ensure_ai_flag_stamps_missing_field_on_legacy_note():
    """Root cause of the 'all patches reverted' bug: user notes predating the
    `AI` convention lack the field, and the OFM lint fails the whole note on a
    patch. ensure_ai_flag stamps `AI: true` (honest provenance) so the lint passes.
    """
    from silica.kernel import ofm
    legacy = "---\ntags:\n  - statistica\n---\n# Varianza\nLa varianza misura la dispersione."
    stamped = ensure_ai_flag(legacy)
    assert "AI: true" in stamped.split("---")[1]
    assert not any("AI" in v for v in ofm.ofm_lint(stamped, stem="Varianza")["violations"])


def test_ensure_ai_flag_is_conservative():
    """Idempotent; never overwrites the user's own AI value; no-op without frontmatter."""
    legacy = "---\ntags:\n  - x\n---\n# n\nbody"
    once = ensure_ai_flag(legacy)
    assert ensure_ai_flag(once) == once            # idempotent
    assert once.count("AI: true") == 1
    user_false = "---\nAI: false\ntags:\n  - x\n---\n# n\nbody"
    assert ensure_ai_flag(user_false) == user_false  # keeps user's explicit value
    assert ensure_ai_flag("# no frontmatter\nbody") == "# no frontmatter\nbody"
