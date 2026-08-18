"""silica_anneal: mechanical sweep of all deferred bundles + escalation steer."""
import orjson

LONG = (
    "Il pattern publish/subscribe disaccoppia produttori e consumatori tramite "
    "un broker che smista i messaggi per topic su reti inaffidabili. " * 4
)


def _park(monkeypatch, tmp_path):
    """Point the deferred store at a temp dir and return it."""
    from silica.kernel.recall import deferred

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


def test_anneal_steer_validates_on_the_same_evidence_that_rejected(tmp_vault, tmp_path, monkeypatch):
    # Finding 2, steer edition: _steer_bundle used to validate the escalation
    # model's "fix" with EMPTY payloads, so a hallucinated op sailed through the
    # weaker gate. Measured live: a promotion bundle came back as an invented
    # encyclopedia note (in Danish) with zero facts from the source. The fix
    # must pass the bundle's persisted payloads, same as silica_deferred_retry.
    from silica.tools import pipeline

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    payloads = [{"batches": [{"inbox_file": "inbox/c.md", "concepts": [
        {"name": "Broker", "inbox_excerpt": "solo Broker è definito qui"},
    ]}]}]
    store.put(
        "ggg7", "inbox/c.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "c.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": "corto"}],
        rejection_reasons={"Reti/Broker.md": "snippet too short"},
        phase="VALIDATE",
        payloads=payloads,
    )

    class _Resp:  # the model pivots to a concept the evidence never grounded
        text = orjson.dumps([{
            "op": "write", "heading": "Ghost", "source_basename": "c.md",
            "path": "Reti/Ghost.md", "title": "Ghost", "snippet": LONG,
        }]).decode()

    class _Provider:
        def call_llm(self, messages, tools=None, **kw):
            return _Resp()

    monkeypatch.setattr("silica.agent.providers.get_provider", lambda *a, **k: _Provider())

    res = pipeline.silica_anneal(steer=True)

    [row] = res["results"]
    assert row["steer"]["status"] == "no_fix", row
    assert res["written"] == 0
    assert store.get("ggg7") is not None  # still parked, never written
    import pytest as _pytest
    from silica.driver import DRIVER
    with _pytest.raises(Exception):
        DRIVER.read_note("Reti/Ghost.md")


def test_anneal_steer_offers_and_honors_the_body_appendix(tmp_vault, tmp_path, monkeypatch):
    """The steer turn is free text (no constrained decode), so it is the one
    seam where the Body Appendix is executable: bodies outside the JSON keep
    single backslashes, and a repair cannot JSON-corrupt a healthy body. The
    prompt must OFFER the format (a model won't invent it), and the parse
    chain must honor it end-to-end."""
    from silica.tools import pipeline

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    store.put(
        "hhh8", "inbox/e.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "e.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": "corto"}],
        rejection_reasons={"Reti/Broker.md": "snippet too short"},
        phase="VALIDATE",
    )

    prompts = []
    body = LONG + "\nVincolo: $\\top \\neq \\frac{1}{2}$."

    class _Resp:
        text = (
            orjson.dumps([{
                "op": "write", "heading": "Broker", "source_basename": "e.md",
                "path": "Reti/Broker.md", "title": "Broker", "snippet_ref": 1,
            }]).decode()
            + "\n===SILICA-BODY 1===\n" + body
        )

    class _Provider:
        def call_llm(self, messages, tools=None, **kw):
            prompts.append(messages[0]["content"])
            return _Resp()

    monkeypatch.setattr("silica.agent.providers.get_provider",
                        lambda *a, **k: _Provider())

    res = pipeline.silica_anneal(steer=True)

    assert "===SILICA-BODY" in prompts[0]
    assert res["written"] == 1
    from silica.driver import DRIVER
    note = DRIVER.read_note("Reti/Broker.md").content
    assert "$\\top \\neq \\frac{1}{2}$" in note
    assert "\t" not in note


def test_anneal_steer_prompt_lists_allowed_headings(tmp_vault, tmp_path, monkeypatch):
    # The heading gate only admits headings named in the payloads, but the
    # steer model never saw that list — it re-conceptualized freely and lost
    # the whole retry to mechanical rejections (17 of 55 deferrals on the
    # 2026-08-05 run). The prompt must carry the allowed names.
    from silica.tools import pipeline

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    payloads = [{"batches": [{"inbox_file": "inbox/c.md", "concepts": [
        {"name": "Broker", "inbox_excerpt": "il Broker smista i messaggi"},
        {"name": "Topic", "inbox_excerpt": "il Topic raggruppa per argomento"},
    ]}]}]
    store.put(
        "iii9", "inbox/c.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "c.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": "corto"}],
        rejection_reasons={"Reti/Broker.md": "snippet too short"},
        phase="VALIDATE",
        payloads=payloads,
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

    assert "ALLOWED HEADINGS" in prompts[0]
    assert "- Broker" in prompts[0] and "- Topic" in prompts[0]
    assert res["written"] == 1


def test_anneal_steer_output_gets_the_sanitize_repairs(tmp_vault, tmp_path, monkeypatch):
    # The steer path used to feed the model's JSON straight to parse_ops,
    # skipping normalize_ops entirely — over-escaped LaTeX (`\\top`, `\\{`)
    # landed verbatim in the vault (8 committed notes, 2026-08-05). The
    # bundle's own excerpts anchor the per-site collapse.
    from silica.tools import pipeline

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    payloads = [{"batches": [{"inbox_file": "inbox/c.md", "concepts": [
        {"name": "Broker", "inbox_excerpt": "vincolo $\\top$ e insieme $\\{a\\}$"},
    ]}]}]
    store.put(
        "jjja", "inbox/c.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "c.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": "corto"}],
        rejection_reasons={"Reti/Broker.md": "snippet too short"},
        phase="VALIDATE",
        payloads=payloads,
    )

    class _Resp:  # JSON body over-escaped by the model INSIDE the string
        text = orjson.dumps([{
            "op": "write", "heading": "Broker", "source_basename": "c.md",
            "path": "Reti/Broker.md", "title": "Broker",
            "snippet": LONG + " Vincolo: $\\\\top$ su $\\\\{a\\\\}$.",
        }]).decode()

    class _Provider:
        def call_llm(self, messages, tools=None, **kw):
            return _Resp()

    monkeypatch.setattr("silica.agent.providers.get_provider", lambda *a, **k: _Provider())

    res = pipeline.silica_anneal(steer=True)
    assert res["written"] == 1

    from silica.driver import DRIVER
    note = DRIVER.read_note("Reti/Broker.md").content
    assert "$\\top$" in note and "$\\{a\\}$" in note
    assert "\\\\top" not in note and "\\\\{" not in note


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


# --- recovered writes must be revertible and traceable (2026-08-18) ----------
# The boundary anneal runs in the FSM's `finally`, after CLEANUP has flushed
# the journal and closed the manifest. Measured on a 3-paper library gate:
# 5 of 94 notes existed on disk with no undo inverse and no provenance record —
# `/revert` walked past them and `check_renucleate` could not see them.

def test_recovered_writes_land_in_the_undo_journal(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.pipeline import silica_anneal

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    store.put(
        "ddd4", "inbox/d.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "d.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": LONG}],
        rejection_reasons={"Reti/Broker.md": "lint failed (stale)"},
        phase="VALIDATE",
    )

    assert silica_anneal()["written"] == 1

    journal = get_undo_journal()
    run_id = journal.last_active_run()
    assert run_id, "the anneal opened no journal run"
    assert "Reti/Broker.md" in {inv.path for inv, _ in journal.inverses_for(run_id)}


def test_recovered_writes_are_appended_to_provenance(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.write.provenance import read_records
    from silica.tools.pipeline import silica_anneal

    tmp_vault.note("Reti/Reti.md", "# Reti\n")
    store = _park(monkeypatch, tmp_path)
    store.put(
        "eee5", "inbox/e.md", "Reti", None,
        [{"op": "write", "heading": "Broker", "source_basename": "e.md",
          "path": "Reti/Broker.md", "title": "Broker", "snippet": LONG}],
        rejection_reasons={"Reti/Broker.md": "lint failed (stale)"},
        phase="VALIDATE",
    )

    assert silica_anneal()["written"] == 1

    recovered = [r for r in read_records() if r.get("sha256") == "eee5"]
    assert recovered, "no provenance record for the recovered source"
    assert any("Reti/Broker" in n for r in recovered for n in r["notes"])
    assert recovered[0]["source"] == "e.md"


# --- who owns the recovered writes (2026-08-19) -----------------------------

def _recovery_args():
    from types import SimpleNamespace

    from silica.kernel.write.ops import InverseOp, InverseOpKind, Op, OpType

    txn = SimpleNamespace(inverses=[
        InverseOp(kind=InverseOpKind.delete_created, path="Reti/PubSub.md")])
    ops = [Op(op=OpType.write, heading="PubSub", source_basename="a.md",
              path="Reti/PubSub.md", snippet=LONG)]
    return txn, ops, {"source_path": "Inbox/a.md"}


class _FakeJournal:
    def __init__(self):
        self.started, self.recorded = [], []

    def start_run(self, **kw):
        self.started.append(kw)
        return "fresh-anneal-run"

    def record(self, run_id, inverse, post_hash):
        self.recorded.append(run_id)


def test_the_boundary_anneal_rides_the_run_it_fires_inside(tmp_vault, monkeypatch):
    """The anneal runs in the FSM's `finally`, so a journal run of its own gets
    a LATER started_at — and `last_active_run` orders by that. /revert therefore
    undid the handful of recovered notes and left the whole nucleation on disk.
    The ledger id is separate and must be the FSM's progress run: that is what
    Coordinator._sweep_dangling_links matches on."""
    from silica.agent import commit as commit_mod
    from silica.kernel.write import undo_journal
    from silica.kernel.write.provenance import read_records
    from silica.tools import pipeline

    journal = _FakeJournal()
    monkeypatch.setattr(undo_journal, "get_undo_journal", lambda: journal)

    txn, ops, bundle = _recovery_args()
    undo_tok = commit_mod._current_undo_run.set("fsm-undo-run")
    ledger_tok = commit_mod._current_ledger_run.set("fsm-progress-run")
    try:
        pipeline._record_recovered_writes(txn, ops, "sha-x", bundle)
    finally:
        commit_mod._current_ledger_run.reset(ledger_tok)
        commit_mod._current_undo_run.reset(undo_tok)

    assert journal.started == [], "opened a journal run that outranks the FSM's"
    assert journal.recorded == ["fsm-undo-run"]
    rec = [r for r in read_records() if r.get("sha256") == "sha-x"]
    assert rec and rec[0]["run_id"] == "fsm-progress-run"


def test_a_standalone_retry_still_opens_its_own_revertible_unit(tmp_vault, monkeypatch):
    from silica.kernel.write import undo_journal
    from silica.kernel.write.provenance import read_records
    from silica.tools import pipeline

    journal = _FakeJournal()
    monkeypatch.setattr(undo_journal, "get_undo_journal", lambda: journal)

    txn, ops, bundle = _recovery_args()
    pipeline._record_recovered_writes(txn, ops, "sha-y", bundle)

    assert journal.started and journal.started[0]["source"] == "anneal"
    assert journal.recorded == ["fresh-anneal-run"]
    rec = [r for r in read_records() if r.get("sha256") == "sha-y"]
    assert rec and rec[0]["run_id"] == "anneal"
