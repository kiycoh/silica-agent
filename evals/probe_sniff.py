# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sniff decision A/B: the form sniff's LLM call vs a typed decision (Jev).

Arms on the same 2000-char heads, every arm sees the same wording
(`forms._FORM_CRITERIA`):

  A  production sniff: free-text one word, `forms._sniff_llm(text)`
  B  structured sniff: constrained decoding, `forms._sniff_llm(text, structured=True)`
  C  Jev alone: argmax of `decide()`'s choice (unsure allowed)
  D  composed, exactly what ships (`forms.sniff_form`): C when its
     probability exceeds tau and the choice is a form, else A. Computed
     offline from C and A, so D pays no extra calls; its latency is C + A on
     fallback.

tau is chosen on the calibration half (smallest tau whose accepted-error rate
is at or under arm A's error rate on that half) and reported on the
verification half. Halves are split by source document, stratified by label.
`--reuse` recomputes every table from the saved rows without a single call.

Labels are the author's (2026-09-19); `synthetic` items were written for the
Italian forms the corpus lacks (transcript, clip, draft) and are reported
separately. Needs the production model endpoint and OPENROUTER_API_KEY.

Run:  uv run python -m evals.probe_sniff [--limit N] [--workers 4] [--reuse]
Out:  bench/sniff_decision_ab.json (raw, untracked) and
      docs/evaluation/sniff-decision-ab-<date>.json (rows without text, tracked)
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get("SNIFF_VAULT", "~/Documents/Obsidian/test")).expanduser()
LABELS = ("study", "transcript", "clip", "draft", "unsure")
DECISION_MODEL = "typesafe/jev-1.13"
HEAD = 2000


@dataclass
class Item:
    id: str
    group: str
    label: str
    lang: str
    synthetic: bool
    text: str


def _head(path: Path, *, strip_frontmatter: bool = False) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if strip_frontmatter and text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = text[end + 4:].lstrip("\n")
    # Never let a label leak into the text under test.
    text = re.sub(r"^form:.*\n", "", text, flags=re.M)
    return text[:HEAD]


def _files(items: list[Item], paths: list[Path], label: str, lang: str, prefix: str,
           **kw) -> None:
    for p in paths:
        if p.is_file():
            items.append(Item(f"{prefix}:{p.stem[:40]}", f"{prefix}:{p.stem}", label, lang,
                              False, _head(p, **kw)))


# --- synthetic Italian items (the corpus has no Italian transcript/clip/draft) ---
SYNTHETIC = [
    ("transcript", "it", "syn-tr-lezione-chimica",
     "Allora, ehm, buongiorno a tutti. Oggi riprendiamo da dove eravamo rimasti la volta "
     "scorsa, cioè dai legami covalenti. Allora, un legame covalente, come dicevamo, si forma "
     "quando due atomi condividono una coppia di elettroni... sì? Domanda? Ah, no, era il "
     "microfono. Ok. Quindi, condividono una coppia di elettroni. Adesso, la differenza con il "
     "legame ionico, che abbiamo visto martedì, è che nel legame ionico c'è un trasferimento, "
     "non una condivisione. Ehm, vi faccio l'esempio del cloruro di sodio, che è quello classico. "
     "Il sodio cede un elettrone, il cloro lo prende. Fine. Nel caso dell'acqua invece... allora "
     "prendiamo l'ossigeno, che ha sei elettroni di valenza, e due idrogeni... aspettate che lo "
     "scrivo alla lavagna. Ecco. Ognuno di questi due condivide un elettrone con l'ossigeno, e "
     "così l'ossigeno arriva a otto. Ottetto, l'abbiamo detto mille volte. Poi c'è il discorso "
     "della polarità, che è quello che vi frega all'esame, quindi state attenti."),
    ("transcript", "it", "syn-tr-riunione-condominio",
     "Appunti riunione condominiale, 14 marzo, sala al piano terra. Presenti: Rossi (int. 3), "
     "Bianchi (int. 7), Ferrara (amministratore), io.\n\n"
     "- caldaia: Ferrara dice che il preventivo della ditta è 8.400, l'altro non è arrivato\n"
     "- Rossi vuole aspettare il secondo preventivo, Bianchi no, vuole chiudere entro aprile\n"
     "- votazione rimandata alla prossima assemblea\n"
     "- ascensore: fermo dal 2, il tecnico passa lunedì (dice Ferrara, da verificare)\n"
     "- io ho chiesto delle infiltrazioni al garage -> Ferrara manda il perito, data non fissata\n"
     "- spese: Bianchi contesta la ripartizione del portone, rimanda al regolamento art. 12\n"
     "- chiusura ore 20:40, prossima assemblea entro fine aprile, convocazione via mail\n\n"
     "da fare: chiedere a Ferrara copia del preventivo, controllare art. 12"),
    ("transcript", "it", "syn-tr-telefonata-cliente",
     "Telefonata con Marco (Studio Adami), 9:30, durata circa venti minuti. Note prese al volo.\n\n"
     "vogliono il sito rifatto entro settembre, budget 'intorno ai 6' (mila, credo)\n"
     "tre sezioni: chi siamo, servizi, contatti + area riservata per i clienti\n"
     "area riservata: solo documenti in pdf, niente pagamenti\n"
     "hosting: non gli interessa, lo scelgo io\n"
     "ho detto che mando una proposta entro venerdì, lui ok\n"
     "ha chiesto se facciamo anche le foto -> ho detto no, gli passo un contatto\n"
     "richiamare per il logo: hanno solo un jpg piccolo, serve il vettoriale\n"
     "dice che il vecchio sito lo aveva fatto il nipote, non hanno accessi, DA RECUPERARE"),
    ("clip", "it", "syn-clip-articolo-suolo",
     "Salvato da https://www.esempio-rivista.it/ambiente/suolo-vivo il 3 maggio.\n\n"
     "Un cucchiaino di terreno sano contiene più organismi viventi di quanti esseri umani "
     "abitino il pianeta. Batteri, funghi, protozoi, nematodi e artropodi formano una rete "
     "trofica che trasforma la sostanza organica in nutrienti disponibili per le piante. Quando "
     "questa rete si impoverisce, per compattazione, lavorazioni profonde ripetute o uso "
     "eccessivo di fertilizzanti di sintesi, il suolo perde struttura e capacità di trattenere "
     "acqua. Gli agronomi intervistati concordano su un punto: la fertilità non si misura solo "
     "in azoto, fosforo e potassio, ma nella quantità di carbonio organico stabile presente nei "
     "primi trenta centimetri.\n\nIl pezzo prosegue con tre casi studio in Emilia, Puglia e "
     "Piemonte, dove aziende agricole hanno adottato la semina su sodo e le cover crop.\n\n"
     "mia nota: interessante per il pezzo sull'orto, verificare il dato del cucchiaino."),
    ("clip", "it", "syn-clip-newsletter",
     "Dalla newsletter settimanale di un blog di economia, copiato il paragrafo che mi serve:\n\n"
     "> Il punto che quasi tutti i commentatori hanno mancato è che il calo dei tassi non "
     "> riduce il costo del debito già contratto a tasso fisso: aiuta chi deve rifinanziare, "
     "> non chi ha già fissato. Per le famiglie con mutuo a tasso fisso stipulato nel 2023 "
     "> l'effetto è nullo, salvo surroga. Per le imprese con linee a breve, invece, l'effetto "
     "> arriva nel giro di un trimestre.\n\n"
     "Fonte: newsletter del 12 aprile, autore non indicato, link nell'email.\n"
     "Da rileggere quando scrivo la sezione sui mutui."),
    ("clip", "it", "syn-clip-forum",
     "Copiato da un thread del forum di fotografia (utente 'lucag'), lo tengo perché spiega "
     "bene la cosa:\n\n"
     "\"Il problema non è il sensore, è l'ottica. Un 50mm f/1.8 di trent'anni fa su un corpo "
     "moderno da 45 megapixel mostra tutti i suoi limiti ai bordi, ma al centro è ancora "
     "eccellente. Se fotografi ritratti chiudi a f/2.8 e non te ne accorgi. Se fotografi "
     "architettura con le linee ai bordi, ti serve un'ottica progettata per quel sensore, punto. "
     "La gente compra corpi macchina e poi si lamenta della nitidezza, quando il collo di "
     "bottiglia è quasi sempre il vetro davanti.\"\n\n"
     "Commento mio: vale anche per il mio 35, provare a f/4 prima di venderlo."),
    ("draft", "it", "syn-draft-script-video",
     "SCRIPT ep. 12, bozza 2 (da rileggere ad alta voce, tagliare a 8 minuti)\n\n"
     "APERTURA\n"
     "Vi hanno detto che per imparare una lingua servono dieci minuti al giorno. È falso, e "
     "oggi vi spiego perché con i numeri. [TODO: inserire dato sulle ore totali, cercare fonte]\n\n"
     "PARTE 1\n"
     "Il metodo dei dieci minuti funziona per una cosa sola: non smettere. Ma non smettere non "
     "è imparare. Qui metto l'esempio del mio francese: due anni di app, zero conversazioni "
     "reali. [forse troppo personale? chiedere a Giulia]\n\n"
     "PARTE 2\n"
     "Cosa funziona invece: input massiccio + output forzato. Spiegare la differenza. Esempio "
     "del podcast a 1.5x. (da riscrivere, troppo lungo)\n\n"
     "CHIUSURA\n"
     "?? non mi convince, provare a chiudere con la domanda iniziale rovesciata."),
    ("draft", "it", "syn-draft-saggio",
     "Introduzione (terza stesura, ancora non mi piace)\n\n"
     "C'è un'idea comoda secondo cui la memoria sarebbe un archivio: si deposita, si "
     "conserva, si recupera. Chiunque abbia provato a ricordare il nome di una via percorsa "
     "mille volte sa che non è così. La memoria ricostruisce, e ricostruendo inventa. Questo "
     "saggio parte da qui, ma non voglio che parta da qui in modo così scolastico. Forse "
     "meglio iniziare con l'aneddoto della stazione, poi arrivare alla tesi.\n\n"
     "[nota per me: la citazione di Bartlett va nel secondo capitolo, non qui. Togliere la "
     "frase sull'archivio o tenerla come bersaglio polemico? Rileggere domani.]\n\n"
     "Tesi: che ogni ricordo sia un atto e non un oggetto. Argomento 1... argomento 2 "
     "(quello sui testimoni oculari, controllare le fonti)..."),
    ("draft", "it", "syn-draft-voiceover",
     "voiceover puntata 7, prova 3, non montata, ci sono ancora gli ehm\n\n"
     "allora ehm la cosa che nessuno ti dice quando apri un negozio online è che il problema "
     "non è vendere è spedire. cioè no, aspetta, rifaccio l'attacco. la cosa che nessuno ti "
     "dice è che il problema non è vendere, è quello che succede dopo. ecco meglio. ehm. "
     "quindi nel primo mese ho perso tre pacchi, due li ha persi il corriere e uno l'ho perso "
     "io perché ho scritto l'indirizzo sbagliato. e qui volevo mettere il pezzo sul software "
     "ma forse è troppo presto, lo sposto dopo. ehm. la seconda cosa è il servizio clienti, "
     "che pensavo fosse rispondere alle email e invece è... no questa frase non funziona, la "
     "riscrivo. riprendo da 'la seconda cosa'."),
    ("unsure", "it", "syn-unsure-tabella-spese",
     "data,voce,importo,categoria,note\n"
     "2026-03-02,spesa supermercato,84.30,alimentari,\n"
     "2026-03-02,benzina,60.00,auto,\n"
     "2026-03-04,abbonamento treno,120.00,trasporti,mensile\n"
     "2026-03-05,farmacia,18.90,salute,\n"
     "2026-03-07,cena fuori,45.00,svago,con Anna\n"
     "2026-03-09,bolletta luce,71.20,casa,bimestrale\n"
     "2026-03-11,libri,32.00,cultura,\n"
     "2026-03-12,spesa supermercato,66.10,alimentari,\n"
     "2026-03-15,regalo,40.00,altro,compleanno Luca\n"
     "2026-03-18,benzina,55.00,auto,\n"
     "2026-03-20,abbonamento streaming,12.99,svago,mensile\n"
     "2026-03-22,spesa supermercato,91.40,alimentari,\n"),
]


def build_items() -> list[Item]:
    items: list[Item] = []
    # study, Italian: the ML course notes (one document per lecture)
    _files(items, sorted((VAULT / "Inbox/machine_learning_md").glob("Lezione *.md")),
           "study", "it", "lez")
    fv = REPO / "evals/forms_vault/Inbox"
    _files(items, [fv / "study-notes-color-science.md"], "study", "en", "fv")
    papers = REPO / "docs/research/papers/md"
    _files(items, [
        papers / "02-rag-and-text/2202.03629v7-survey-of-hallucination-in-natural-language-generation.md",
        papers / "01-retrieval/1901.04085v5-passage-re-ranking-with-bert.md",
        papers / "03-agents-and-memory/2210.03629v3-react-synergizing-reasoning-and-acting-in-language-models.md",
        papers / "03-agents-and-memory/2302.04761v1-toolformer-language-models-can-teach-themselves-to-use-tools.md",
    ], "study", "en", "paper")
    # transcript: typed call notes are terminal records (spec boundary)
    _files(items, [fv / "raw-sponsor-call-2026-06-12.md"], "transcript", "en", "fv")
    # clip: a clipping, and /web evidence banks (quotes + the owner's why)
    _files(items, [fv / "clipped-article-inference-pricing.md"], "clip", "en", "fv")
    _files(items, sorted((REPO / "docs/evaluation/gates/l2-2026-08-06/vault/sources").glob("*.md")),
           "clip", "en", "web", strip_frontmatter=True)
    # draft: the VO pass (spec boundary) and plans still being worked
    _files(items, [fv / "transcript-dump-ep180-rough.txt"], "draft", "en", "fv")
    _files(items, [REPO / "docs/plans" / n for n in (
        "capabilities-workers-refactor.md", "ponytail-debt-fixes.md",
        "web-research-plan-steering.md", "graph-tree-focus.md")], "draft", "en", "plan")
    # unsure: code, and session handoffs (a record, but authored and finished)
    _files(items, [REPO / "silica/agent/decide.py"], "unsure", "en", "code")
    _files(items, [REPO / "docs/handoffs" / n for n in (
        "2026-06-10-steering-refactor.md", "2026-06-19-keyphrase-optimization.md")],
           "unsure", "en", "handoff")
    for label, lang, sid, text in SYNTHETIC:
        items.append(Item(sid, sid, label, lang, True, text))
    return items


def split(items: list[Item], seed: int = 7) -> tuple[set[str], set[str]]:
    """Group-level halves, stratified by label: (calibration ids, verification ids)."""
    rng = random.Random(seed)
    calib: set[str] = set()
    by_label: dict[str, list[str]] = {}
    for it in items:
        by_label.setdefault(it.label, []).append(it.group)
    for label, groups in by_label.items():
        groups = sorted(set(groups))
        rng.shuffle(groups)
        calib.update(groups[: len(groups) // 2])
    return ({it.id for it in items if it.group in calib},
            {it.id for it in items if it.group not in calib})


def _timed(fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        err = ""
    except Exception as e:  # an arm's failure is a data point, not a crash
        out, err = None, f"{type(e).__name__}: {str(e)[:120]}"
    return out, round(time.perf_counter() - t0, 3), err


_LAST = threading.local()


def _recording_call_llm(real):
    """Wrap forms.call_llm so each arm's completion tokens and reply length
    are recorded (why arm A takes 13 s where B takes 1 s is a data point)."""
    def _call(*a, **k):
        resp = real(*a, **k)
        u = resp.usage or {}
        _LAST.usage = {"completion_tokens": u.get("completion_tokens"),
                       "reply_chars": len(resp.text or ""),
                       "finish": resp.finish_reason}
        return resp
    return _call


def run_item(it: Item) -> dict:
    import silica.kernel.forms as forms
    from silica.agent.decide import decide

    a, ta, ea = _timed(lambda: forms._sniff_llm(it.text))
    ua = getattr(_LAST, "usage", None)
    b, tb, eb = _timed(lambda: forms._sniff_llm(it.text, structured=True))
    ub = getattr(_LAST, "usage", None)
    c, tc, ec = _timed(lambda: decide(
        {"file_content": it.text},
        {"form": {"type": "choice", "instructions": forms._SNIFF_INSTRUCTIONS,
                  "criteria": forms._FORM_CRITERIA}}))
    jev = (c or {}).get("form") or {}
    return {**asdict(it), "text": None,
            "A": {"pred": a or "unsure", "s": ta, "err": ea, "usage": ua},
            "B": {"pred": b or "unsure", "s": tb, "err": eb, "usage": ub},
            "C": {"pred": jev.get("choice", "unsure"), "s": tc, "err": ec,
                  "p": (jev.get("probabilities") or {}).get(jev.get("choice"), 0.0),
                  "probabilities": jev.get("probabilities"), "confidence": jev.get("confidence")}}


def _norm(p: str) -> str:
    return p if p in LABELS else "unsure"


def compose(row: dict, tau: float) -> tuple[str, float, bool]:
    """(pred, full-path seconds, accepted) for arm D at tau: the production
    rule of forms._sniff_decide (strictly above tau, fallback = arm A)."""
    c = row["C"]
    if c["pred"] in ("study", "transcript", "clip", "draft") and c["p"] > tau:
        return c["pred"], c["s"], True
    return row["A"]["pred"], c["s"] + row["A"]["s"], False


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 2)


def arm_metrics(rows: list[dict], preds: list[str], secs: list[float]) -> dict:
    errors = [(r["label"], p) for r, p in zip(rows, preds) if _norm(p) != r["label"]]
    per_class: dict[str, dict] = {}
    for r, p in zip(rows, preds):
        d = per_class.setdefault(r["label"], {"n": 0, "wrong": 0, "as": {}})
        d["n"] += 1
        if _norm(p) != r["label"]:
            d["wrong"] += 1
            d["as"][_norm(p)] = d["as"].get(_norm(p), 0) + 1
    return {"n": len(rows), "errors": len(errors),
            "accuracy": round(1 - len(errors) / len(rows), 3) if rows else None,
            "per_class": per_class, "p50_s": _pct(secs, 0.5), "p95_s": _pct(secs, 0.95)}


def choose_tau(rows: list[dict]) -> tuple[float, list[dict]]:
    """Smallest tau whose accepted-error rate on `rows` is <= arm A's error rate
    there; 1.01 (never accept) when none. Returns (tau, the sweep)."""
    b_err = sum(_norm(r["A"]["pred"]) != r["label"] for r in rows) / max(1, len(rows))
    sweep = []
    for tau in sorted({r["C"]["p"] for r in rows} | {0.5, 0.6, 0.7, 0.8, 0.9, 0.95}):
        acc = [(r, compose(r, tau)) for r in rows]
        accepted = [(r, d) for r, d in acc if d[2]]
        wrong_acc = sum(_norm(d[0]) != r["label"] for r, d in accepted)
        rate = wrong_acc / len(accepted) if accepted else 0.0
        sweep.append({"tau": round(tau, 3), "accepted": len(accepted),
                      "accepted_wrong": wrong_acc, "accepted_error_rate": round(rate, 3),
                      "D_accuracy": round(sum(_norm(d[0]) == r["label"] for r, d in acc) / len(rows), 3)})
    ok = [s for s in sweep if s["accepted_error_rate"] <= b_err and s["accepted"] > 0]
    return (min(s["tau"] for s in ok) if ok else 1.01), sweep


def report(rows: list[dict], tau: float) -> dict:
    out = {}
    for arm in ("A", "B", "C"):
        out[arm] = arm_metrics(rows, [r[arm]["pred"] for r in rows], [r[arm]["s"] for r in rows])
        out[arm]["call_errors"] = sum(bool(r[arm]["err"]) for r in rows)
    d = [compose(r, tau) for r in rows]
    out["D"] = arm_metrics(rows, [x[0] for x in d], [x[1] for x in d])
    accepted = [(r, x) for r, x in zip(rows, d) if x[2]]
    out["D"].update({"tau": tau, "accepted": len(accepted),
                     "fallback_rate": round(1 - len(accepted) / len(rows), 3) if rows else None,
                     "accepted_wrong": sum(_norm(x[0]) != r["label"] for r, x in accepted)})
    return out


def main() -> int:
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
    workers = int(args[args.index("--workers") + 1]) if "--workers" in args else 4
    from silica.config import CONFIG
    import silica.kernel.forms as forms
    CONFIG.decision_model = DECISION_MODEL
    forms.call_llm = _recording_call_llm(forms.call_llm)
    items = build_items()
    if limit:
        items = items[:limit]
    raw = REPO / "bench/sniff_decision_ab.json"
    t0 = time.time()
    if "--reuse" in args:
        prior = json.loads(raw.read_text(encoding="utf-8"))
        rows, seconds = prior["rows"], prior["seconds"]
        items = [it for it in items if it.id in {r["id"] for r in rows}]
    else:
        print(f"{len(items)} items, model={CONFIG.model}, decision={DECISION_MODEL}", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(run_item, items))
        seconds = round(time.time() - t0, 1)
    calib_ids, verify_ids = split(items)
    for r in rows:
        r["half"] = "calibration" if r["id"] in calib_ids else "verification"
    calib = [r for r in rows if r["id"] in calib_ids]
    verify = [r for r in rows if r["id"] in verify_ids]
    tau, sweep = choose_tau(calib)
    result = {
        "date": "2026-09-19", "model": CONFIG.model, "decision_model": DECISION_MODEL,
        "items": len(rows), "seconds": seconds,
        "tau": tau, "sweep_calibration": sweep,
        "calibration": report(calib, tau), "verification": report(verify, tau),
        "all": report(rows, tau),
        "real_only": report([r for r in rows if not r["synthetic"]], tau),
        "italian_only": report([r for r in rows if r["lang"] == "it"], tau),
        "rows": rows,
    }
    raw.parent.mkdir(exist_ok=True)
    raw.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    # The tracked copy: ids, labels, halves and every arm's answer, no text,
    # so the evidence outlives the untracked bench/ folder.
    out = REPO / f"docs/evaluation/sniff-decision-ab-{result['date']}.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    for half in ("calibration", "verification", "all", "real_only", "italian_only"):
        print(f"\n== {half} ==")
        for arm, m in result[half].items():
            extra = f" tau={m['tau']} fallback={m['fallback_rate']} accepted_wrong={m['accepted_wrong']}" if arm == "D" else ""
            print(f"  {arm}: acc={m['accuracy']} err={m['errors']}/{m['n']} p50={m['p50_s']}s p95={m['p95_s']}s{extra}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
