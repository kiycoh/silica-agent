# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/vault, /status, /settings: which vault this is and how it stands."""
from __future__ import annotations

import json

from silica.command_table import Call, Result
from silica.config import CONFIG
from silica.onboarding.adopt import switch_vault


def vault(c: Call) -> Result:
    """/vault [<path>] — show the active vault, or adopt another one."""
    args, out = c.args, c.out
    from silica.driver import driver_kind

    arg = " ".join(args).strip()
    if arg:
        r = switch_vault(arg)
        if r.error:
            out.print(f"  [red]Cannot adopt as a vault — {r.error}[/]")
            return out
        if r.created:
            out.print(f"  Created [bold]{r.vault}[/] as the session vault.")
        if r.write_dir:
            out.print(
                f"  Source tree — writes confined to [bold]{r.write_dir}/[/]; the rest of "
                "the vault is read-only context. Change `write_dir` in vault.yaml."
            )
        if r.seeded_ignore:
            out.print("  Created [bold].silicaignore[/] — add folders to keep out of the index.")
        if r.invalid_write_dir:
            out.print(
                "  [red]⚠ vault.yaml declares an invalid `write_dir` — every write "
                "will be rejected until it is a relative path inside the vault.[/]"
            )
        out.print(f"  Vault → [bold]{r.vault}[/] (backend: {driver_kind()})")
        if r.repo_warning:
            out.print(f"  [yellow]⚠ {r.repo_warning}[/]")
        # Surface the frozen-language drift here, not only in `/vault` info:
        # a switch is exactly when a wrong-frozen store (english on an IT
        # vault) would otherwise stay silent. Reuses the doctor's check.
        if r.language_drift:
            out.print(
                f"  [yellow]⚠ Language: {r.language}, co-occurrence store "
                f"frozen {r.store_language} — run /cooccur --force to rebuild.[/]"
            )
        out.print(
            "  [dim]Index namespace follows the vault — run /embed and /cooccur "
            "if this vault has not been indexed yet.[/]"
        )
        return out
    vault = CONFIG.vault_path or "(not configured)"
    out.print(f"  Vault:   [bold]{vault}[/]")
    out.print(f"  Backend: {driver_kind()}")
    if CONFIG.vault_path:
        from pathlib import Path

        count = len(list(Path(CONFIG.vault_path).rglob("*.md")))
        out.print(f"  Notes:   {count}")
        from silica.kernel.vault_language import language_status

        lang, store_lang, drift = language_status(CONFIG.vault_path)
        if lang and drift:
            out.print(
                f"  Language: {lang} (store frozen: {store_lang} "
                "⚠ — run /cooccur --force to rebuild)"
            )
        elif lang and store_lang:
            out.print(f"  Language: {lang} (store: {store_lang})")
        elif lang:
            out.print(f"  Language: {lang}")
    return out


def status(c: Call) -> Result:
    """/status [run_id] — progress of the newest run, or of the one named.

    The bare form now opens with the narration's view of THIS session
    (ticket 12): running spans, context pressure, spend — the same fold the
    surfaces render, so /status cannot disagree with them. The ledger digest
    below it stays: it answers recovery, which narration refuses to carry.
    """
    args, out = c.args, c.out
    if not args:
        from silica.obs import narration as _narr
        sid = _narr.NARRATOR.sid
        if sid is not None:
            st = _narr.fold_all(_narr.read_beats(_narr.narration_dir() / f"{sid}.jsonl"))
            running = [sv for sv in st.spans.values() if sv.ended_ts is None]
            out.print(f"  session [bold]{sid}[/] · beat {st.cursor}"
                      f" · context {st.context_tokens:,} tok"
                      f" · spent {st.cost_tokens:,} tok out")
            for sv in running[:10]:
                out.print(f"    [cyan]{sv.kind}[/] {sv.summary} [dim](running)[/]")
            if st.gaps:
                out.print(f"  [yellow]gaps in the record: {st.gaps}[/]")
    from silica.tools import TOOLS

    run_id = args[0] if (len(args) + 1) > 1 else ""
    result = TOOLS["silica_ledger_digest"].run(run_id=run_id)
    try:
        parsed = json.loads(result)
        digest = parsed.get("digest", result)
        # Preformatted plain text: Markdown would reflow every line into one
        # paragraph, and markup would eat the "[16 checkpoints]" brackets.
        out.print(str(digest), markup=False, highlight=False)
    except Exception:
        # Not the digest's JSON: said as it came, since it is all there is.
        out.print(result)
    # E(vault) cache line — written by /report (write_report). No cache
    # file → nothing shown; /status never triggers a VaultReport itself.
    try:
        from pathlib import Path as _EP
        energy_file = _EP(CONFIG.vault_path or "") / ".silica" / "energy.json"
        if energy_file.is_file():
            e = json.loads(energy_file.read_text(encoding="utf-8"))
            line = f"  E(vault): [bold]{e['value']:+.2f}[/]"
            if e.get("prev") is not None:
                line += f"  (delta {e['value'] - e['prev']:+.2f} since last report)"
            out.print(line)
            # Attribute the delta: the six contributions sum to the total, so
            # naming the terms that moved says WHICH force changed the vault.
            # Movers only — an unchanged term is noise on this line.
            terms, prev_terms = e.get("terms") or {}, e.get("prev_terms") or {}
            movers = sorted(
                ((t, v - prev_terms[t]) for t, v in terms.items() if t in prev_terms),
                key=lambda kv: -abs(kv[1]),
            )
            movers = [(t, d) for t, d in movers if abs(d) >= 0.01]
            if movers:
                out.print(
                    "    moved: " + ", ".join(f"{t} {d:+.2f}" for t, d in movers[:4]),
                    markup=False,
                )
    except Exception:
        # A torn energy.json only loses this one courtesy line: the cache is
        # /report's, and /status must answer whether or not it can be read.
        pass
    return out


def settings(c: Call) -> Result:
    """/settings [<key> <value|none>] — read or edit vault.yaml without the wizard."""
    args, out = c.args, c.out
    # View/edit vault.yaml without the wizard. ponytail: safe_dump rewrite —
    # YAML comments are not preserved; hand-edit the file to keep them.
    import yaml as _yaml
    from pathlib import Path as _P
    from silica.kernel.base.manifest import MANIFEST_REL, reset_manifest_cache
    _KEYS = {"cooccurrence_lang", "conventions.language",
             "conventions.reply_language", "conventions.max_tags"}
    mf = _P(CONFIG.vault_path) / MANIFEST_REL
    data = _yaml.safe_load(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    if not isinstance(data, dict):
        data = {}
    if not args:
        out.print(f"  [bold]{mf}[/]")
        body = _yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip() \
            if data else "(defaults — no vault.yaml yet)"
        out.print(f"  {body}")
        out.print(f"  Keys: {', '.join(sorted(_KEYS))} — /settings <key> <value|none>")
        return out
    if len(args) != 2 or args[0] not in _KEYS:
        return out.refused(f"usage /settings <key> <value|none>. Keys: {', '.join(sorted(_KEYS))}")
    key, raw = args
    val = None if raw.lower() in ("none", "null") else (int(raw) if raw.isdigit() else raw)
    node = data
    *heads, leaf = key.split(".")
    for h in heads:
        if not isinstance(node.get(h), dict):
            node[h] = {}
        node = node[h]
    if val is None:
        node.pop(leaf, None)
    else:
        node[leaf] = val
    # Atomic: a torn vault.yaml parses as defaults, and default write_dir=""
    # is the whole vault root — the exact boundary this file exists to set.
    from silica.kernel.base.durable import atomic_write_bytes
    atomic_write_bytes(mf, _yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False).encode("utf-8"))
    reset_manifest_cache()
    out.print(f"  {key} = {raw} → {mf.name} (comments in the file are not preserved)")
    return out
