# Silica — Addendum al Review Architetturale: Sezioni da Implementare (da S2.3)

> Sostituisce gli step S2.3 → S3.3 del §6 e introduce i **contratti normativi condivisi** (C1–C5) che quegli step assumono.
> I contratti chiudono le cause-radice di B1–B7. Implementa C1–C5 *prima* di S2.3: gli step vi fanno riferimento, non li ripetono.
> Data: 2026-05-25 · Stato: normativo salvo dove marcato *(aperto)*.

---

## Parte A — Contratti normativi condivisi

Sono trasversali a tutta la pipeline. Vivono in `silica/kernel/ops.py` (lo schema) e nel Protocol del Driver (freshness, transazionalità). Sono *single source of truth*: nessun modulo ridefinisce questi concetti localmente.

### C1 — Schema canonico delle Op (chiude B1)

> **Decisione cristallizzata (ADR-007).** L'operazione è un modello Pydantic unico, importato da sanitize / validate / snapshot / bulk / lint. Nessun modulo accede ai campi via stringhe libere; chi deriva la nota toccata lo fa **solo** da `path`/`heading`, **mai** da un campo `name` (che non esiste).

```python
# silica/kernel/ops.py
from enum import Enum
from pydantic import BaseModel, Field

class OpType(str, Enum):
    write     = "write"      # crea nota nuova (path NON deve esistere)
    patch     = "patch"      # arricchisce nota esistente (path DEVE esistere)
    overwrite = "overwrite"  # riscrive nota esistente preservando identità/history
    delete    = "delete"     # cancella (solo via tool wrapped + confirm)
    skip      = "skip"       # no-op esplicito (conta nel denominatore del gate? → no, vedi C4)

class Op(BaseModel):
    op: OpType
    heading: str                       # concetto; chiave di provenienza col payload
    source_basename: str               # file inbox da cui deriva (per validate)
    path: str | None = None            # vault-relative; obbligatorio per write/patch/overwrite/delete
    snippet: str = ""                  # corpo distillato (write/patch)
    hub: str | None = None             # [[Hub]] obbligatorio per write
    content: str | None = None         # corpo intero (solo overwrite)
    tags: list[str] | None = None
    related: list[str] | None = None

    def touched_ref(self) -> str | None:
        """Nota toccata dall'op. UNICA via per lint/snapshot di derivare il ref."""
        return self.path  # mai self.name — il campo non esiste
```

**Conseguenze normative**
- `silica_sanitize` ritorna `list[Op]` validato, non un dict grezzo.
- Il gate `LINT` e `silica_snapshot` derivano le note toccate **esclusivamente** da `Op.touched_ref()` (questo elimina B1 alla radice: niente più `op["name"]` vuoto).
- Un'op senza `path` su un `op_type` che lo richiede è un **reject** in validate, non un silent-skip a valle.

### C2 — Freshness contract come post-condizioni per-operazione (chiude B5)

> **Decisione cristallizzata (ADR-008).** Il contratto read-after-write non è un generico "settle": è una post-condizione *specifica per operazione*, identica sui due backend. `_wait_for_settle` verifica la post-condizione, non la mera leggibilità del file. **Ogni** mutazione la invoca, non solo `create`.

| Operazione | Post-condizione da verificare in poll (timeout 2 s) |
|---|---|
| `create(path, content)` | `read(path)` ok **e** `content` riflesso (non solo file esistente) |
| `overwrite(path, content)` | `read(path).content` riflette `content` |
| `set_prop(ref, k, v)` | `property:read(ref, k) == v` |
| `move(a → b)` | `read(b)` ok **∧** `read(a)` fallisce **∧** cache backlink aggiornata |
| `append(ref, c)` | `c` presente in `read(ref).content` |
| `delete(path)` | `read(path)` fallisce |

```python
def _wait_for_settle(self, postcond: Callable[[], bool], timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if postcond():
                return
        except RuntimeError:
            pass
        time.sleep(0.1)
    logger.warning("Settle timeout — postcondition non soddisfatta entro %.1fs", timeout)
    # NB: timeout su una mutazione critica è un errore, non un warning silenzioso (vedi C4 abort)
```

### C3 — Transazionalità e rollback (chiude B2 e B7)

> **Decisione cristallizzata (ADR-009).** Il `Txn` registra l'**inverso** di ogni op, non solo un numero di versione. `patch`/`enrich` **DEVE** preservare identità e history della nota — `delete()+create()` è **vietato** come implementazione di patch/overwrite. Serve un primitivo atomico `DRIVER.overwrite()` mappato su `obsidian create … overwrite=true`.

```python
class InverseOp(BaseModel):
    kind: str          # "delete_created" | "restore_version" | "recreate_deleted"
    path: str
    version: int | None = None       # per restore_version
    prior_content: str | None = None # per recreate_deleted (snapshot del corpo pre-delete)

class Txn(BaseModel):
    id: str
    inverses: list[InverseOp]
```

**Regole di costruzione del `Txn` (in `silica_snapshot`, prima della WRITE):**
- op `write` (path nuovo) → inverso `delete_created(path)` — *non* restore (non c'è versione precedente).
- op `patch`/`overwrite` (path esistente) → snapshot della versione corrente → inverso `restore_version(path, N)`.
- op `delete` → snapshot del corpo corrente → inverso `recreate_deleted(path, prior_content)`.

**Driver — nuovo primitivo (sostituisce il delete+create in `bulk.py`):**
```python
def overwrite(self, ref, content: str) -> None:
    """Riscrive in-place preservando identità/history. CLI: create … overwrite=true.
    VIETATO implementarlo come delete()+create() — distrugge la history su cui si
    fonda restore_version (ADR-009)."""
```
`bulk.execute_operations` per `patch`: `DRIVER.overwrite(path, patch_snippet(...))`. Mai delete.

### C4 — Accoppiamento gate ↔ executor + propagazione esiti (chiude B3 e B4)

> **Decisione cristallizzata (ADR-010).** Esiste **un solo artefatto di ops** che attraversa `validate → snapshot → write`. Il validator riscrive l'artefatto con gli ops **coerced + deduped**; snapshot e write leggono *quello*, mai l'input pre-validazione. Ogni stage ritorna un esito strutturato che il router tratta come abort su fallimento *parziale*, non solo su `"error"` top-level.

- `silica_validate_ops` produce `validated_ops: list[Op]` e li **persiste** sul `ops_path` canonico (overwrite del file). `snapshot` e `bulk_write` ricevono lo **stesso** `ops_path`.
- Denominatore del gate ≥10%: rigetti / (ops *azionabili*); `skip` espliciti esclusi dal denominatore.
- `execute_operations` ritorna `{"ok": bool, "failed": [...]}`. **Contratto router:** `if not res["ok"]: → ROLLBACK`. Una write parzialmente fallita è un fallimento del batch, mai un Success.

### C5 — Idempotenza e marker `done/` (chiude B4, lato persistenza)

> **Decisione cristallizzata (ADR-011).** Il file inbox si sposta in `done/` **solo** se tutti i gate sono verdi *e* tutte le write sono riuscite. Un ledger SQLite registra l'esito per-op per rendere i resume sicuri.

- `silica/kernel/ledger.py` — tabella `ops(txn_id, source_basename, path, op, status, ts)`.
- CLEANUP (`move → done/`) è raggiungibile **solo** dallo stato terminale `DONE`, mai da `ERROR`/`ROLLBACK`.
- Resume: prima di processare un inbox file, il router interroga il ledger; un `source_basename` con ops `committed` non viene riprocessato.

---

## Parte B — Step aggiornati

### Step 2.3 — End-to-end single-file inject *(aggiornato)*

**Obiettivo**: un concetto entra nel vault end-to-end con i gate **reali** che sparano e un rollback che ripristina *davvero* — incluso il caso di nota appena creata.

> [!WARNING]
> Differenze rispetto alla bozza originale: (1) `DELEGATE` non è più `dummy_ops` — chiama il Distiller reale via `silica/agent/delegate.py` con `prep_delegation` (verbatim + payload-by-pointer + SHA-256); (2) tutti i passaggi consumano `list[Op]` (C1); (3) `LINT` e `SNAPSHOT` derivano le note da `Op.touched_ref()`, non da `name`; (4) `WRITE` legge l'artefatto **validato** (C4); (5) `ROLLBACK` applica gli `InverseOp` (C3), quindi cancella le note create; (6) i `NamedTemporaryFile` si chiudono in un `try/finally` o si usa una `workdir` per-txn ripulita a fine run.

**Deliverable**
- `silica/agent/delegate.py` cablato nello stato `DELEGATE` (un solo worker in S2.3, fan-out a S3.1).
- `silica/kernel/ops.py` (C1) usato da sanitize/validate/snapshot/bulk/lint.
- `DRIVER.overwrite()` (C3) + `bulk.execute_operations` che usa `overwrite` per i patch.
- `silica_validate_ops` che **riscrive** `ops_path` con gli ops validati (C4).
- `silica_snapshot` che costruisce un `Txn` di `InverseOp` (C3) — incluso `delete_created` per i `write`.
- `DRIVER.restore(txn)` che applica gli inversi (delete delle note create, restore_version dei patch).
- `ledger.py` (C5) + CLEANUP raggiungibile solo da `DONE`.
- Pulizia temp file (no leak in `/tmp`).

**Criterio di accettazione** (tre prove, tutte obbligatorie)
1. **Happy path**: `silica> ingerisci l'inbox di oggi in Deep Learning` → nota creata/arricchita, inbox spostato in `done/`, ledger con ops `committed`.
2. **Gate spara su input cattivo**: payload con >10% di rigetti → abort **prima** della write, vault invariato, inbox **non** spostato.
3. **Rollback reale su fallimento post-write** (es. lint rosso dopo una `write` riuscita): la nota creata viene **cancellata** (non "restore a versione 0" inesistente), una nota patchata torna alla versione precedente con history intatta, inbox **non** spostato.

---

### Step 3.1 — Partizionamento + fan-out *(aggiornato)*

**Obiettivo**: Injector con partizionamento >200 concetti / >80 KB e fan-out parallelo sicuro.

> [!WARNING]
> Aggiunte rispetto alla bozza: (1) thread-safety del `DRIVER` — l'init lazy va reso atomico (eager init prima del fan-out, **oppure** `threading.Lock` in `get_driver()`), altrimenti N worker creano N backend in race; (2) il merge multi-batch dedup-a sui `path` riusando la logica di C4, non re-implementandola; (3) backoff esponenziale sulle chiamate LLM rate-limited.

**Deliverable**
- `silica/kernel/partition.py` — partizionamento deterministico (>200 concetti **o** >80 KB → split); golden test sul determinismo dell'ordine.
- `silica/agent/delegate.py` — `ThreadPoolExecutor` max 7 worker, hard-stop a 10 (già presente), **+ backoff** su errori transitori.
- Init `DRIVER` reso thread-safe (eager o lock).
- Merge dei risultati multi-batch → `list[Op]` unico, deduplicato per `path` (C4).

**Criterio di accettazione**: inbox >200 concetti → partizionato → distillato in parallelo → merge → un solo artefatto di ops validato → write. Nessuna race sul Driver sotto 7 worker (test con `ThreadSanitizer`/stress run). Il merge non produce due ops sullo stesso `path`.

---

### Step 3.2 — Graph-diff gate *(aggiornato)*

**Obiettivo**: non-regressione misurabile a livello di grafo, come **gate post-lint** con rollback.

> [!WARNING]
> Prerequisito bloccante: il backend `fs` e il **test di parità fs-vs-cli (S1.4)** devono esistere e passare. Il graph-diff è affidabile solo se i due backend producono lo stesso `GraphSnapshot` — altrimenti diffi rumore di implementazione, non regressioni reali. Se S1.4 non è stato completato, va chiuso prima di S3.2 (è il checkpoint architetturale, non un optional).

**Deliverable**
- `silica/kernel/graphdiff.py` — `diff(before: GraphSnapshot, after: GraphSnapshot) → Regressions`.
- `GraphSnapshot` **incrementale**: cattura solo i nodi toccati dal batch + il loro 1-hop neighborhood (orphans/unresolved/backlink locali), non l'intero grafo (mitigazione R3 su vault grandi).
- Regole di regressione: `nuovi_orfani == 0` ∧ `Δ unresolved ≤ 0` ∧ `nessun backlink preesistente rotto`.
- Integrazione nel router come gate **dopo** `LINT`, **prima** di `CLEANUP`; fallimento → `ROLLBACK` (C3).

**Criterio di accettazione**: un batch che introduce un orfano (es. write senza `[[Hub]]` raggiungibile) → il gate spara → rollback applica gli inversi → grafo identico allo snapshot `before`. Snapshot incrementale su vault grande resta sotto soglia di tempo accettabile.

---

### Step 3.3 — YAML recipe engine *(aggiornato)*

**Obiettivo**: pipeline come ricette YAML dichiarative, con **parità verificata** rispetto al router hardcoded.

> [!WARNING]
> Vincoli aggiunti: (1) il recipe engine esegue gli **stessi contratti** C1–C5 — una fase `gate` che fallisce instrada a `rollback`, una `mechanical` che fallisce parzialmente è abort (C4); il YAML non è una scorciatoia per saltare i gate; (2) i tool referenziati dalla ricetta devono **esistere**: `silica_restore` e `silica_cleanup` oggi sono citati in `injector.yaml` ma non implementati — vanno creati come tool reali (oggi il router usa `DRIVER.restore` e `silica_move` diretti); (3) `silica_snapshot` non deve passare oggetti Python attraverso il confine JSON del registry — il `Txn` si serializza (è già un `BaseModel` in C3), niente `_txn_obj` leaky.

**Deliverable**
- `silica/router/recipe_parser.py` — parser del DAG YAML; mapping `kind ∈ {mechanical, semantic, gate, txn}` → transizioni FSM.
- `silica_restore` e `silica_cleanup` come tool reali (chiudono i riferimenti orfani della ricetta).
- `Txn` serializzabile end-to-end (no `_txn_obj`).
- `recipes/injector.yaml` eseguito dal router al posto del codice hardcoded.

**Criterio di accettazione**: `recipes/injector.yaml` produce **risultati identici** al router hardcoded di S2.2 sugli stessi input (golden test di parità router-vs-ricetta). Una ricetta che fa fallire un gate produce lo stesso rollback del percorso hardcoded.

---

## Parte C — Mappa di conformità (verifica delle tue fix)

| Bug | Causa-radice | Chiuso da | Verifica |
|---|---|---|---|
| B1 — gate lint/snapshot no-op | `op["name"]` inesistente | C1 (`Op.touched_ref()`) | Lint gira su ≥1 nota in S2.3 prova 3 |
| B2 — patch = delete+create | nessun primitivo overwrite | C3 (`DRIVER.overwrite`, divieto delete+create) | History intatta dopo un patch |
| B3 — gate ↔ executor scollegati | `ops_path` non riscritto | C4 (artefatto unico validato) | Write su path esistente coerced a patch *e* eseguita |
| B4 — partial-write = Success | nessun esito propagato | C4 (`res["ok"]`) + C5 (done/ solo da DONE) | Inbox non archiviato su write parziale |
| B5 — settle placebo | post-condizione generica | C2 (post-cond per-op, tutte le mutazioni) | `set_prop` poi `property:read` converge |
| B6 — oracolo fs assente | S1.4 non chiuso | Prereq di S3.2 | `tests/golden/test_driver_parity.py` verde |
| B7 — rollback di create impossibile | snapshot = versione | C3 (`delete_created`) | Nota creata cancellata in S2.3 prova 3 |

**Inelegances residue da chiudere lungo il percorso** (non bloccanti, ma annotate): `Txn` serializzabile (no `_txn_obj`), cleanup dei temp file / workdir per-txn, init `DRIVER` thread-safe, escaping del `content` su CLI verso un meccanismo robusto se gli argomenti grandi superano `ARG_MAX` (valutare stdin invece di `content=` inline), allineamento doc/codice sulla selezione backend (`SILICA_BACKEND` vs `CONFIG.backend`).