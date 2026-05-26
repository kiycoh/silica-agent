"""Golden tests for driver parity (fs vs cli).

This validates that the FS backend produces the exact same results as the CLI backend
on a real vault, ensuring the headless oracle mode is perfectly compatible.
"""
import os
import unicodedata
import pytest

from silica.driver.cli_backend import ObsidianCLIBackend
from silica.driver.fs_backend import ObsidianFSBackend

# The real vault to test against (must be open in Obsidian)
VAULT_NAME = "Alex's Second Brain Sync"
VAULT_PATH = "/home/kiycoh/Documents/Obsidian/Alex's Second Brain Sync"


@pytest.fixture(scope="module")
def backends():
    if not os.path.exists(VAULT_PATH):
        pytest.skip(f"Test vault path not found: {VAULT_PATH}")
    
    cli = ObsidianCLIBackend(vault_name=VAULT_NAME)
    fs = ObsidianFSBackend(vault_path=VAULT_PATH)
    return cli, fs


def test_parity_search_names(backends):
    cli, fs = backends
    # Query a common letter
    cli_res = cli.search_names("a")
    fs_res = fs.search_names("a")
    
    cli_names = {unicodedata.normalize('NFC', r.name) for r in cli_res}
    fs_names = {unicodedata.normalize('NFC', r.name) for r in fs_res}
    
    assert fs_names == cli_names


def is_markdown_target(target: str) -> bool:
    return not target.lower().endswith(
        ('.png', '.jpg', '.jpeg', '.pdf', '.webp', '.svg', '.gif', '.mp4', '.zip', '.html', '.css', '.js')
    )


def normalize_name(name: str) -> str:
    name = unicodedata.normalize('NFC', name).lower()
    name = name.replace('"', '').replace("'", "").replace("’", "").replace("`", "")
    name = name.rstrip('\\').strip()
    return name


# Explicit allow-list for differences in Obsidian's internal indexer logic
# that cannot be reconciled purely through string normalization.
KNOWN_PARITY_DIVERGENCES = set()


def test_parity_orphans(backends):
    cli, fs = backends
    cli_res = cli.orphans()
    fs_res = fs.orphans()
    
    cli_names = {normalize_name(r.name) for r in cli_res if r.path.endswith('.md')}
    fs_names = {normalize_name(r.name) for r in fs_res if r.path.endswith('.md')}
    
    cli_names -= KNOWN_PARITY_DIVERGENCES
    fs_names -= KNOWN_PARITY_DIVERGENCES
    
    diff_fs_cli = fs_names - cli_names
    diff_cli_fs = cli_names - fs_names
    assert fs_names == cli_names, f"Orphans mismatch!\nFS but not CLI: {diff_fs_cli}\nCLI but not FS: {diff_cli_fs}"


def test_parity_unresolved(backends):
    cli, fs = backends
    cli_res = cli.unresolved()
    fs_res = fs.unresolved()
    
    cli_targets = {normalize_name(r.target) for r in cli_res if is_markdown_target(r.target)}
    fs_targets = {normalize_name(r.target) for r in fs_res if is_markdown_target(r.target)}
    
    cli_targets -= KNOWN_PARITY_DIVERGENCES
    fs_targets -= KNOWN_PARITY_DIVERGENCES
    
    diff_fs_cli = fs_targets - cli_targets
    diff_cli_fs = cli_targets - fs_targets
    assert fs_targets == cli_targets, f"Unresolved links mismatch!\nFS but not CLI: {diff_fs_cli}\nCLI but not FS: {diff_cli_fs}"


def test_parity_read_note(backends):
    cli, fs = backends
    # Find an arbitrary note from files
    files = cli.list_files()
    if not files:
        pytest.skip("No files in vault to test read_note")
        
    ref = files[0]
    
    cli_nc = cli.read_note(ref)
    fs_nc = fs.read_note(ref)
    
    assert cli_nc.content == fs_nc.content


def test_parity_links_and_backlinks(backends):
    cli, fs = backends
    files = cli.list_files()
    if not files:
        pytest.skip("No files in vault to test links")
        
    # Pick a file with links if possible
    test_ref = None
    for ref in files:
        if cli.links(ref):
            has_md_links = any(is_markdown_target(r.path or r.name) for r in cli.links(ref))
            if has_md_links:
                test_ref = ref
                break
            
    if not test_ref:
        test_ref = files[0]
        
    cli_links = {normalize_name(r.name) for r in cli.links(test_ref) if is_markdown_target(r.path or r.name)}
    fs_links = {normalize_name(r.name) for r in fs.links(test_ref) if is_markdown_target(r.path or r.name)}
    assert fs_links == cli_links
    
    cli_backlinks = {normalize_name(r.name) for r in cli.backlinks(test_ref) if r.path.endswith('.md')}
    fs_backlinks = {normalize_name(r.name) for r in fs.backlinks(test_ref) if r.path.endswith('.md')}
    assert fs_backlinks == cli_backlinks
