# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Hot-path audit A13 — delete/rename must drop the embedding vector too.

`delete` and `move` patched the link/graph index but never touched
`EmbedStore`, so `cosine_top_k` kept returning the removed/old-path note as a
live candidate until the next full `/embed` rebuild (a deleted note wastes a
slot; a renamed note can appear twice).
"""
import silica.driver as dm
from silica.kernel.recall import embed


def test_a13_delete_drops_embed_vector(tmp_vault):
    embed.clear()
    tmp_vault.note("Notes/Ghost.md", "# Ghost\n\nbody text")
    store = embed.get_store()
    store.upsert("Notes/Ghost", "Ghost", [0.1, 0.2, 0.3])
    assert store.get_vec("Notes/Ghost") is not None

    dm.DRIVER.delete("Notes/Ghost.md")

    assert embed.get_store().get_vec("Notes/Ghost") is None, \
        "deleted note left a phantom embedding vector in the candidate set"


def test_a13_move_drops_old_embed_vector(tmp_vault):
    embed.clear()
    tmp_vault.note("Notes/Old.md", "# Old\n\nbody text")
    store = embed.get_store()
    store.upsert("Notes/Old", "Old", [0.1, 0.2, 0.3])

    dm.DRIVER.move("Notes/Old.md", "Notes/New.md")

    assert embed.get_store().get_vec("Notes/Old") is None, \
        "renamed note left a stale old-key vector (would appear twice in candidates)"


def test_a13_holds_over_the_bridge_too(tmp_vault):
    """Settles the ws half of A13. The run-end sweep skips a transport with no
    mtimes, so while the plugin is attached nothing else would remove them."""
    from pathlib import Path

    from silica.config import CONFIG
    from silica.driver.ws_backend import ObsidianWSBackend
    from silica.kernel.recall import sync
    from silica.kernel.vault import Vault

    transport = ObsidianWSBackend("ws://127.0.0.1:0")

    def plugin(method, **params):
        note = Path(CONFIG.vault_path) / params["path"]
        if method == "delete":
            note.unlink()
        elif method == "move":
            note.rename(Path(CONFIG.vault_path) / params["to"])
        else:
            raise AssertionError(method)

    transport._rpc = plugin
    transport._is_graph_built = True
    vault = Vault(transport)
    embed.clear()
    tmp_vault.note("Notes/Ghost.md", "# Ghost\n")
    tmp_vault.note("Notes/Old.md", "# Old\n")
    store = embed.get_store()
    store.upsert("Notes/Ghost", "Ghost", [0.1, 0.2, 0.3])
    store.upsert("Notes/Old", "Old", [0.1, 0.2, 0.3])

    dm.set_driver(transport)
    assert sync.sweep(force=True).skipped  # why this cannot wait for the sweep
    dm.set_driver(None)

    vault.delete("Notes/Ghost.md")
    vault.move("Notes/Old.md", "Notes/New.md")

    assert embed.get_store().get_vec("Notes/Ghost") is None
    assert embed.get_store().get_vec("Notes/Old") is None
