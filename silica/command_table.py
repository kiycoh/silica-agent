# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a command is, one row each, and what running one gives back.

A table and nothing else, as `view_table` and `provider_table` are, because
`config` reads it to say what a process withholds and the handlers beside their
subject (`sources/mail_cli`, `connectors/connect_cli`) return its `Result`: this
module has to sit below all of them. So a row names its handler by its dotted
path and `silica.commands.dispatch` imports it when a line asks for it.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class Command:
    name: str
    group: str       # "workflow" | "direct" | "system"
    usage: str
    summary: str
    examples: tuple[str, ...] = ()
    # REPL-only: driven by the terminal session itself, so the web GUI cannot run
    # it as a chat turn (it has its own affordance instead — new chat, the header
    # panel, the thinking toggle). Kept out of the GUI's command picker.
    repl_only: bool = False
    # The first words after the name that make a line able to carry a secret
    # (`/connect add` takes a pasted entry with its API key in it). The REPL's
    # history never records such a line: ~/.silica/history is plain text kept
    # for years, and a Class B secret lives in the keyring only (ADR-0089
    # point 7). Named here, beside the usage that takes the secret, so a new
    # verb that takes one is marked where it is written.
    secret_verbs: tuple[str, ...] = ()
    # `module:function`, reached through `silica.commands.dispatch`. Empty for
    # what no line of the table runs: the REPL's own session commands, and the
    # two a turn answers ahead of the table (TURN_COMMANDS).
    handler: str = ""
    # How the words are read: shlex, for a command whose arguments are paths and
    # quoted intents, or `words.direct_args`, where an apostrophe is a letter.
    shlex: bool = False
    # The view this command goes with (a key of view_table.VIEWS): a process
    # that does not serve the view does not run it (ADR-0140 point 4).
    view: str = ""
    # Refused by a demo beyond its views (ADR-0139 point 6): what reaches the
    # internet, brings files in, spends the model or the embedder outside the
    # chat's 8 calls, or needs a second vault or a settings page.
    demo_off: bool = False
    # Old names the dispatcher still runs, withheld with the row and offered nowhere:
    # /learn was the command's name until ADR-0141 named the view Memorize, and
    # typed from habit it still runs.
    aliases: tuple[str, ...] = ()


@dataclass
class Result:
    """What a command gave back, for a surface to render.

    `lines` are what it said, each the arguments of a `Console.print`, so the words
    keep their marks and each surface draws them its own way. A surface that shows
    work as it happens hands in `live`: `/nucleate` runs for hours, and a question
    has to follow what it asks about. The line is kept either way, and it is
    printed at once only there.

    The rest says how the command ended. With none of them set it did the whole
    job. `directive`: the model is asked this. `error`: why the line cannot run,
    shown as an error and asked of nobody. `answer`: Markdown that is the reply
    itself, which a code block would not render (the formulas on a card). `saved`:
    a write to memory, `user_memory.save`'s record, which the browser draws with its
    Undo. `batch`: the event a REPL's panel opens on.
    """

    live: Callable[..., None] | None = None
    lines: list[tuple[tuple, dict]] = field(default_factory=list)
    directive: str | None = None
    error: str | None = None
    answer: str | None = None
    saved: dict | None = None
    batch: Any = None

    # `ingest`, `mail_cli` and the rest print to whatever console they are given,
    # and a renderer asks this before it opens a live region.
    is_terminal: ClassVar[bool] = False

    def print(self, *objects: Any, **style: Any) -> None:
        if self.live is not None:
            self.live(*objects, **style)
        elif style.get("markup", True):
            # A `[/x]` raises where it was said, as a console's print does: /find's
            # own except answers that with the raw result, and the work after the
            # line must not run.
            from rich.markup import render

            for o in objects:
                if isinstance(o, str):
                    render(o)
        self.lines.append((objects, style))

    def to_model(self, directive: str | None) -> Result:
        """What the model is asked, "" for nothing: a service like `ingest.nucleate`
        answers with one or the other."""
        self.directive = directive or None
        return self

    def refused(self, why: str) -> Result:
        self.error = why
        return self


@dataclass(frozen=True)
class Ask:
    """How a surface puts a question to the person at it. A surface says so, and a
    process cannot: `silica --gui` from a shell has a tty on stdin and its person
    in a browser. Where nobody can answer a surface hands in None, not this."""

    text: Callable[[str], str]    # a line; the prompt's marks are drawn
    line: Callable[[str], str]    # a line; the prompt as written, "[y/N]" and all
    secret: Callable[[str], str]  # typed hidden, in no history (ADR-0089 point 7)
    yes: Callable[[str], bool]


def unseen(out: Result) -> AbstractContextManager:
    """No renderer: an agent loop a command runs itself goes undrawn."""
    return nullcontext(None)


@dataclass(frozen=True)
class Call:
    """One line, on its way to a handler."""

    args: list[str]           # the words after the command, read the way its row says
    raw: str                  # the line as typed: a query or a path keeps its case
    messages: list[dict]      # the conversation, for what acts on it
    out: Result
    ask: Ask | None = None
    # Opens the surface's renderer of an agent loop a command runs itself, given
    # the result to draw into.
    progress: Callable[[Result], AbstractContextManager] = unseen


# A turn answers these two before the table: a persona is worn, not run
# (ADR-0127), and /web is an agent turn with web-only tools, which a handler
# running its own loop would report twice (session.turn.expand_web_turn).
TURN_COMMANDS = ("/persona", "/web")

# A demo's refused skills: a skill has no row, it lists itself from its SKILL.md.
# /explain is off by the owner's answer (Open 6).
DEMO_OFF_SKILLS = ("/explain",)


COMMANDS: tuple[Command, ...] = (
    # Workflow — agent-directed
    Command(
        name="/report",
        group="workflow",
        usage="[folder] [--top-k=N] [--embeddings]",
        summary="structural audit of the vault → steering loop",
        examples=(
            "/report Concepts/ML",
            "/report --embeddings",
            "/report --cooccurrence",
            "/report --top-k=15 --embeddings",
        ),
        handler="silica.commands.review:report", shlex=True,
    ),
    Command(
        name="/nucleate",
        group="workflow",
        usage="<file...> [--target=DIR] [--hub=H]",
        summary="bring files in: notes via Injector FSM, code as skeleton stubs",
        examples=(
            "/nucleate Inbox/note.md --target=Concepts/AI",
            "/nucleate silica/cli.py",
            "/nucleate paper.pdf --target=Concepts/AI",
        ),
        handler="silica.commands.ingress:nucleate", shlex=True, demo_off=True,
    ),
    Command(
        name="/promote",
        group="workflow",
        usage="[<key>]",
        summary="session memory → a note: list what keeps recurring, promote one through the gate",
        examples=("/promote", "/promote user.dog.name"),
        handler="silica.commands.ingress:promote", shlex=True, demo_off=True,
    ),
    Command(
        name="/episodes",
        group="direct",
        usage="[--save=<path>]",
        summary="show what session memory holds: live chains, dated, grouped by key; writes nothing",
        examples=("/episodes", "/episodes --save=~/episodes.md"),
        handler="silica.commands.memory:episodes",
    ),
    Command(
        name="/remember",
        group="direct",
        usage="[text]",
        summary="save your text as typed to this vault's memory, no model involved; "
                "bare, asks silica to keep what you just said",
        examples=("/remember thesis draft due 2026-11-30", "/remember"),
        handler="silica.commands.memory:remember",
    ),
    Command(
        name="/memory",
        group="direct",
        usage="",
        summary="show what silica remembers about you and this vault, with file paths and budgets",
        handler="silica.commands.memory:memory",
    ),
    Command(
        name="/agenda",
        group="direct",
        usage="[today|week|YYYY-MM-DD]",
        summary="per-day merge of events, dated notes, agent activity and review due",
        examples=("/agenda", "/agenda week", "/agenda 2026-09-01"),
        repl_only=True,  # the GUI has its own calendar tab
        handler="silica.commands.reading:agenda", view="calendar",
    ),
    Command(
        name="/convert",
        group="direct",
        usage="<file...> [--target=DIR]",
        summary="transcode a non-.md file (PDF) into a markdown note in the inbox",
        examples=(
            "/convert paper.pdf",
            "/convert paper.pdf --target=Concepts/AI",
        ),
        handler="silica.commands.ingress:convert", shlex=True, demo_off=True,
    ),
    Command(
        name="/web-search",
        group="direct",
        usage='"<concept>" [--max-searches=N]',
        summary="research a concept on the web → cited findings note in the Inbox (then /nucleate)",
        examples=(
            '/web-search "retrieval-augmented generation"',
            '/web-search "graph neural networks" --max-searches=6',
        ),
        handler="silica.commands.ingress:web_search", shlex=True, demo_off=True,
    ),
    Command(
        name="/web",
        group="workflow",
        usage="[keywords]",
        summary="answer from the web instead of the vault, cited; bare /web re-asks your last question",
        examples=(
            "/web graph rewiring benchmarks",
            "/web",
        ),
        demo_off=True,
    ),
    Command(
        name="/keep",
        group="direct",
        usage="",
        summary="save the last /web answer as a cited note in the Inbox (then /nucleate)",
        handler="silica.commands.ingress:keep", demo_off=True,
    ),
    Command(
        name="/fetch",
        group="direct",
        usage="<url>",
        summary="read one URL (YouTube gives its transcript) → verbatim note in the Inbox",
        examples=(
            "/fetch https://arxiv.org/abs/2005.11401",
            "/fetch https://www.youtube.com/watch?v=aircAruvnKk",
        ),
        handler="silica.commands.ingress:fetch", shlex=True, demo_off=True,
    ),
    Command(
        name="/organize",
        group="workflow",
        usage='"<intent>" [--scope=FOLDER] [--file=taxonomy.yaml] [--merge] [--move-uncategorized] [--apply]',
        summary="classify and reorganize vault notes according to a taxonomy",
        examples=(
            '/organize "put AI notes in Concepts/AI, cooking notes in Life"',
            '/organize "archive Acme docs under Clients/Acme" --merge',
            "/organize --file=taxonomy.yaml --apply",
            "/organize --scope=Inbox",
        ),
        handler="silica.commands.review:organize", shlex=True, demo_off=True,
    ),
    # Reader — agent-directed, strictly read-only (output in chat, never writes).
    # /summarize /explain /compare /relate /schematize /diagram are skills and
    # list themselves through skill_commands() below.
    Command(
        name="/quiz",
        group="workflow",
        usage="[note|folder] [--n=10]",
        summary="active-recall quiz; graded answers resurface the notes you miss. No target = review queue",
        examples=("/quiz Concepts/ML --n=5", "/quiz"),
        handler="silica.commands.learn:quiz", shlex=True, view="learn",
    ),
    Command(
        name="/flashcards",
        group="workflow",
        usage="<note|folder> [--n=12] [--save=<path>]",
        summary="question-and-answer cards from the notes' checked points; --save writes them for Spaced Repetition",
        examples=('/flashcards "Support Vector Machine"', "/flashcards Corso/ML --n=20 --save=Corso/ML/Flashcards.md"),
        handler="silica.commands.learn:flashcards", shlex=True, demo_off=True,
    ),
    Command(
        name="/slides",
        group="workflow",
        usage="<note|folder> [--save=<path>]",
        summary="a slide deck of what the notes say; --save writes it for Obsidian's Slides",
        examples=('/slides "Lezione 13"', '/slides "Lezione 13" --save="Corso/ML/Lezione 13 - slides.md"'),
        handler="silica.commands.learn:slides", shlex=True,
    ),
    Command(
        name="/memorize",
        group="workflow",
        usage="[plan] [--n=8] [--plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]] [--teach <note>] [--status [plan]]",
        summary="a study session graded against the source line, a plan to continue or write, a teach-back, or the readings in one line; the Memorize view is the same engine with a screen",
        examples=("/memorize Concepts/ML", '/memorize "differential forms"'),
        handler="silica.commands.learn:memorize", shlex=True, view="learn", aliases=("/learn",),
    ),
    # Direct — immediate, no LLM round-trip
    Command(
        name="/status",
        group="direct",
        usage="[run_id]",
        summary="progress digest of the last run",
        handler="silica.commands.vault:status",
    ),
    Command(
        name="/embed",
        group="direct",
        usage="[folder] [--force]",
        summary="build/update embedding index",
        handler="silica.commands.index:embed", demo_off=True,
    ),
    Command(
        name="/cooccur",
        group="direct",
        usage="[folder] [--force]",
        summary="build/update co-occurrence index (without embedder)",
        handler="silica.commands.index:cooccur",
    ),
    Command(
        name="/lexical",
        group="direct",
        usage="[folder] [--force]",
        summary="build/update lexical (BM25/fuzzy) index",
        handler="silica.commands.index:lexical",
    ),
    Command(
        name="/wiki",
        group="direct",
        usage="[folder|path] [--overview-only] [--force]",
        summary="behavioral code wiki: ARCHITECTURE.md + one note per subsystem",
        examples=("/wiki", "/wiki kernel", "/wiki silica/kernel",
                  "/wiki /core/src/main/java/io/github/app/manager",
                  "/wiki --overview-only", "/wiki --force"),
        handler="silica.commands.index:wiki", demo_off=True,
    ),
    Command(
        name="/graph",
        group="direct",
        usage="[out.html] [folder]",
        summary="export knowledge graph",
        handler="silica.commands.index:graph",
    ),
    Command(
        name="/map",
        group="direct",
        usage="<note> [--force]",
        summary="radial mind-map rooted on a note → maps/<stem>.canvas",
        handler="silica.commands.index:mind_map",
    ),
    Command(
        name="/find",
        group="direct",
        usage="<query> [--k=N]",
        summary="semantic search",
        handler="silica.commands.reading:find",
    ),
    Command(
        name="/changes",
        group="direct",
        usage="",
        summary="notes this session wrote to, with added/removed line counts",
        repl_only=True,  # the GUI has its own Changes drawer, off the same ledger
        handler="silica.commands.notes:changes",
    ),
    Command(
        name="/undo",
        group="direct",
        usage="[note-path]",
        summary="undo the last patch on a note",
        handler="silica.commands.notes:undo",
    ),
    Command(
        name="/delete",
        group="direct",
        usage="<path>",
        summary="delete a note, even one other notes link to; says which links it left "
                "pointing nowhere, /undo brings it back",
        examples=("/delete Corso/Autoencoders.md",),
        handler="silica.commands.notes:delete",
    ),
    Command(
        name="/trash",
        group="direct",
        usage="[restore <path> | erase <path> | empty]",
        summary="the notes deleted from this vault: bring one back, or delete for good",
        examples=("/trash", "/trash restore Corso/Autoencoders.md", "/trash empty"),
        handler="silica.commands.notes:trash",
    ),
    Command(
        name="/review",
        group="direct",
        usage="[--flush=HASH]",
        summary="inspect the async review queue (deferred ops)",
        handler="silica.commands.review:review",
    ),
    Command(
        name="/anneal",
        group="direct",
        usage="[--steer] [--limit=N]",
        summary="retry every deferred bundle against the current vault; --steer escalates what still fails",
        examples=("/anneal", "/anneal --steer", "/anneal --limit=5"),
        handler="silica.commands.review:anneal", demo_off=True,
    ),
    Command(
        name="/revert",
        group="direct",
        usage="[run-id | --source <file>]",
        summary="revert a whole run (bare: the newest; or the RUN id /status prints, whole or its first characters), or every run derived from one source",
        handler="silica.commands.notes:revert",
    ),
    Command(
        name="/zotero",
        group="direct",
        usage="[file|group|unlink|grant] [--apply|--yes]",
        summary="the linked Zotero library: status, file the inbox, write groups, unlink",
        examples=("/zotero", "/zotero file", "/zotero file --apply", "/zotero group --apply"),
        handler="silica.sources.zotero_cli:command", demo_off=True,
    ),
    Command(
        name="/mail",
        group="direct",
        usage="[import <file.mbox|file.eml> [--label=X] [--me=ADDR] [--all] [--limit=N] [--apply]]",
        summary="mail threads you took part in, from a file you exported, kept as notes; "
                "bare, your addresses and imports so far",
        examples=("/mail", "/mail import ~/Downloads/inbox.mbox",
                  "/mail import takeout.mbox --label=Thesis --apply"),
        handler="silica.sources.mail_cli:command", demo_off=True,
    ),
    Command(
        name="/connect",
        group="direct",
        usage="[list|add <json>|secure <key>|login <key>|logout <key>|remove <key>|reload]",
        summary="MCP connectors: list, add, move inline secrets to the keyring, log in",
        examples=("/connect", "/connect secure github", "/connect login gmail"),
        secret_verbs=("add", "secure"),
        handler="silica.connectors.connect_cli:command", demo_off=True,
    ),
    Command(
        name="/dedup",
        group="direct",
        usage="[folder]",
        summary="deduplicate (sub-agent)",
        handler="silica.commands.review:dedup", shlex=True, demo_off=True,
    ),
    Command(
        name="/curate",
        group="direct",
        usage="[folder] [--apply]",
        summary="curate the vault: plan autolink/orphan/dedup work (dry-run; --apply executes)",
        handler="silica.commands.review:curate", demo_off=True,
    ),
    Command(
        name="/aliases",
        group="direct",
        usage="[folder] [--apply]",
        summary="propose frontmatter aliases for note titles (abbreviations, spellings); dry-run, --apply writes",
        examples=("/aliases", "/aliases Concepts/AI --apply"),
        handler="silica.commands.review:aliases", demo_off=True,
    ),
    Command(
        name="/enrich",
        group="direct",
        usage="[folder]",
        summary="add what lean notes are missing, as an appended section (sub-agent)",
        handler="silica.commands.review:enrich", shlex=True, demo_off=True,
    ),
    Command(
        name="/stale",
        group="direct",
        usage="[--all] | --stamp [folder] [--write]",
        summary="list notes whose documents: sources changed structurally (--all includes cosmetic); --stamp derives documents:/code_ref for unbound notes, --write stamps them",
        handler="silica.commands.reading:stale",
    ),
    Command(
        name="/migrate",
        group="direct",
        usage="[folder] [--write]",
        summary="notes Silica wrote in an older shape, rewritten through today's writer; a list until --write, one revertible run",
        handler="silica.commands.reading:migrate",
    ),
    Command(
        name="/impact",
        group="direct",
        usage="[<git-range>]",
        summary="changed files → affected notes (documenting + 1-hop import neighbors); no range = uncommitted changes",
        handler="silica.commands.reading:impact",
    ),
    Command(
        name="/plans",
        group="direct",
        usage="",
        summary="list plans/ notes grouped by status: (todo|in-progress|blocked|done)",
        handler="silica.commands.reading:plans",
    ),
    Command(
        name="/path",
        group="direct",
        usage="<noteA> <noteB>",
        summary="shortest reading path between two notes (wikilinks + co-occurrence)",
        examples=('/path "RAG" "Transformers"',),
        handler="silica.commands.reading:path",
    ),
    Command(
        name="/contested",
        group="direct",
        usage="",
        summary="list notes flagged contested: true with their unresolved contradictions",
        handler="silica.commands.reading:contested",
    ),
    Command(
        name="/verify",
        group="direct",
        usage='<note> [--clear] [--as "<name>"]',
        summary="vouch for a note as a person (OKF verified, bound to the body); --clear withdraws, --as signs with a name",
        examples=("/verify docs/adr/0038-detection-at-session-open-writes-on-command",),
        handler="silica.commands.notes:verify",
    ),
    Command(
        name="/check",
        group="direct",
        usage="<note>",
        summary="read a note you wrote against the vault: links, identifiers, dates and quotations, by grade; never edits it",
        examples=('/check "Drafts/Opinion on limitation"',),
        handler="silica.commands.notes:check", demo_off=True,
    ),
    # System
    Command(
        name="/sessions",
        group="system",
        usage="[prune <days>d]",
        summary="list saved conversations (narration + legacy); prune deletes old ones",
        examples=("/sessions", "/sessions prune 90d"),
        repl_only=True,
    ),
    Command(
        name="/resume",
        group="system",
        usage="<n|id>",
        summary="reopen a saved conversation and continue it",
        examples=("/resume 1",),
        repl_only=True,
    ),
    Command(
        name="/new",
        group="system",
        usage="",
        summary="start a fresh conversation (same as /clear); the old one stays resumable",
        repl_only=True,
    ),
    Command(
        name="/vault",
        group="system",
        usage="[path]",
        summary="show the active vault, or switch to another for this session",
        handler="silica.commands.vault:vault", demo_off=True,
    ),
    Command(
        name="/settings",
        group="system",
        usage="[<key> <value|none>]",
        summary="view or edit vault.yaml settings (language, tags) without the wizard",
        examples=(
            "/settings",
            "/settings conventions.reply_language italian",
        ),
        handler="silica.commands.vault:settings", shlex=True, demo_off=True,
    ),
    Command(
        name="/help",
        group="system",
        usage="",
        summary="show this help",
        repl_only=True,
    ),
    Command(
        name="/model",
        group="system",
        usage="",
        summary="show the current LLM model",
        repl_only=True,
    ),
    Command(
        name="/tools",
        group="system",
        usage="",
        summary="list registered tools",
        repl_only=True,
    ),
    Command(
        name="/clear",
        group="system",
        usage="",
        summary="reset conversation history",
        repl_only=True,
    ),
    Command(
        name="/verbose",
        group="system",
        usage="",
        summary="cycle tool progress: off → new → all → verbose",
        repl_only=True,
    ),
    Command(
        name="/thinking",
        group="system",
        usage="",
        summary="toggle display of the reasoning block",
        repl_only=True,
    ),
    Command(
        name="/incognito",
        group="system",
        usage="",
        summary="toggle this session off the record: not captured, memory neither shown nor saved",
        handler="silica.commands.memory:incognito",
    ),
    Command(
        name="/persona",
        group="system",
        usage="[name|off]",
        summary="the persona this conversation wears: list them, put one on, take it off",
    ),
    Command(
        name="/exit",
        group="system",
        usage="",
        summary="exit silica",
        repl_only=True,
        aliases=("/quit", "/q"),
    ),
)


ROWS: dict[str, Command] = {n: c for c in COMMANDS for n in (c.name, *c.aliases)}


def demo_refused() -> frozenset[str]:
    """Every command a demo refuses beyond its views, old names included."""
    return frozenset(n for c in COMMANDS if c.demo_off for n in (c.name, *c.aliases)) | set(DEMO_OFF_SKILLS)


def withheld(served: frozenset[str], demo: bool) -> frozenset[str]:
    """The commands of every view not `served`, and in a demo what it refuses."""
    gone = {n for c in COMMANDS if c.view and c.view not in served for n in (c.name, *c.aliases)}
    return frozenset(gone | (demo_refused() if demo else set()))
