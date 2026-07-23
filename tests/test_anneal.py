"""silica_anneal: mechanical sweep of all deferred bundles + escalation steer."""
import orjson

LONG = (
    "Il pattern publish/subscribe disaccoppia produttori e consumatori tramite "
    "un broker che smista i messaggi per topic su reti inaffidabili. " * 4
)


def _park(monkeypatch, tmp_path):
    """Point the deferred store at a temp dir and return it."""
    from silica.kernel import deferred

    monkeypatch.setattr(deferred, "_store_dir", lambda: tmp_path / "deferred")
    deferred._stores.clear()
    return deferred.get_deferred_store()


def test_anneal_sweeps_all_bundles(tmp_vault, tmp_path, monkeypatch):
    from silica.tools.pipeline import silica_anneal

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    # Bundle 1: write op that passes validation now → written, bundle cleared.
    store.put(
        "aaa1", "inbox/a.md", "Reti", None,
        [{"op": "write", "heading": "PubSub", "source_basename": "a.md",
          "path": "Reti/PubSub.md", "title": "PubSub", "snippet": LONG}],
        rejection_reasons={"Reti/PubSub.md": "lint failed (stale)"},
        phase="VALIDATE",
    )
    # Bundle 2: op still failing (snippet under the 100-char gate).
    store.put(
        "bbb2", "inbox/b.md", "Reti", None,
        [{"op": "write", "heading": "Stub", "source_basename": "b.md",
          "path": "Reti/Stub.md", "title": "Stub", "snippet": "troppo corto"}],
        rejection_reasons={"Reti/Stub.md": "snippet too short"},
        phase="VALIDATE",
    )

    res = silica_anneal()

    assert res["bundles"] == 2
    assert res["written"] == 1
    assert res["still_deferred"] == 1
    assert store.get("aaa1") is None          # cleared
    assert store.get("bbb2") is not None      # still parked


def test_anneal_steer_fixes_with_stamped_reason(tmp_vault, tmp_path, monkeypatch):
    from silica.tools import pipeline

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    store.put(
        "ccc3", "inbox/c.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "c.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": "corto"}],
        rejection_reasons={"Reti/Broker.md": "snippet too short"},
        phase="VALIDATE",
    )

    prompts = []

    class _Resp:
        text = orjson.dumps([{
            "op": "write", "heading": "Broker", "source_basename": "c.md",
            "path": "Reti/Broker.md", "title": "Broker", "snippet": LONG,
        }]).decode()

    class _Provider:
        def call_llm(self, messages, tools=None, **kw):
            prompts.append(messages[0]["content"])
            return _Resp()

    monkeypatch.setattr("silica.agent.providers.get_provider", lambda *a, **k: _Provider())

    res = pipeline.silica_anneal(steer=True)

    [row] = res["results"]
    assert row["steer"]["status"] == "committed", row
    assert res["written"] == 1
    assert store.get("ccc3") is None  # written op removed → bundle gone
    # the stamped per-op reason reached the escalation prompt
    assert "snippet too short" in prompts[0]


def test_anneal_recovered_write_is_autolinked_not_orphan(tmp_vault, tmp_path, monkeypatch):
    # The deferred path bypasses the FSM's AUTOLINK and HUB_UPDATE — recovered
    # notes used to land with zero edges and no MOC membership (audit finding
    # 2). They must get inline links AND a hub-MOC bullet now.
    from silica.tools.pipeline import silica_anneal

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    tmp_vault.note("Reti/Broker.md", "# Broker\n\nInstradatore di messaggi.\n")
    store = _park(monkeypatch, tmp_path)
    body = ("Il topic raggruppa i messaggi per argomento; il Broker li smista ai "
            "sottoscrittori interessati mantenendo il disaccoppiamento. " * 4)
    store.put(
        "ddd4", "inbox/d.md", "Reti", "Reti",
        [{"op": "write", "heading": "Topic", "source_basename": "d.md",
          "path": "Reti/Topic.md", "title": "Topic", "snippet": body}],
        phase="VALIDATE",
    )

    res = silica_anneal()
    assert res["written"] == 1

    from silica.driver import DRIVER
    content = DRIVER.read_note("Reti/Topic.md").content
    assert "[[Broker]]" in content  # inline edge to an existing sibling

    hub = DRIVER.read_note("Reti/Reti.md").content
    assert "- [[Topic]]" in hub  # MOC membership, same as the FSM path
    assert "## Da: d" in hub or "## From: d" in hub  # language-aware section


def test_anneal_retry_keeps_grounding_parity_with_persisted_payloads(tmp_vault, tmp_path, monkeypatch):
    # Finding 2 core: the retry used to re-validate with EMPTY payloads, so ops
    # rejected on payload-grounded checks (unknown heading, collision paths)
    # passed on strictly weaker validation. With the bundle's original payloads
    # persisted, the same checks run again and the op stays deferred.
    from silica.tools.pipeline import silica_deferred_retry

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    payloads = [{"batches": [{"inbox_file": "inbox/d.md", "concepts": [
        {"name": "Topic", "inbox_excerpt": "solo Topic è definito qui"},
    ]}]}]
    store.put(
        "eee5", "inbox/d.md", "Reti", "Reti",
        [{"op": "write", "heading": "Ghost", "source_basename": "d.md",
          "path": "Reti/Ghost.md", "title": "Ghost", "snippet": LONG}],
        phase="VALIDATE",
        payloads=payloads,
    )

    res = silica_deferred_retry("eee5")
    assert res.get("success") is False
    assert any("not present in payload" in r["reason"] for r in res["rejected"])
    bundle = store.get("eee5")
    assert bundle is not None                      # still parked
    assert bundle.get("payloads") == payloads      # evidence survives the re-put


def test_anneal_retry_without_payloads_keeps_legacy_behavior(tmp_vault, tmp_path, monkeypatch):
    # Old bundles (pre-schema) carry no payloads: retry still validates
    # payload-free, so they are not bricked by the schema addition.
    from silica.tools.pipeline import silica_deferred_retry

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    store.put(
        "fff6", "inbox/d.md", "Reti", "Reti",
        [{"op": "write", "heading": "Ghost", "source_basename": "d.md",
          "path": "Reti/Ghost.md", "title": "Ghost", "snippet": LONG}],
        phase="VALIDATE",
    )

    res = silica_deferred_retry("fff6")
    assert res.get("success") is True and res["written"] == 1
