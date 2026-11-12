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

    @property
    def synopsis(self) -> str:
        """The command and its usage, as a handler's usage line says it: written
        here once, since the handlers' own copies had drifted from what /help
        and the picker show."""
        return f"{self.name} {self.usage}".rstrip()

    def form(self, word: str) -> str:
        """The optional part of `usage` that starts with `word`, its brackets
        dropped: how a refusal names the one form it was given wrong
        (`--teach <note>` out of /memorize's usage)."""
        at = self.usage.index("[" + word)
        depth = 0
        for i, ch in enumerate(self.usage[at:], at):
            depth += {"[": 1, "]": -1}.get(ch, 0)
            if depth == 0:
                return self.usage[at + 1:i]
        raise ValueError(f"{self.name}: unbalanced brackets in its usage")


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
    itself, which a code block would not render (the formulas on a card); `notes`
    are the lines under it (agent.turn_notes), /council's meter. `saved`:
    a write to memory, `user_memory.save`'s record, which the browser draws with its
    Undo. `batch`: the event a REPL's panel opens on.
    """

    live: Callable[..., None] | None = None
    lines: list[tuple[tuple, dict]] = field(default_factory=list)
    directive: str | None = None
    error: str | None = None
    answer: str | None = None
    notes: list[dict] = field(default_factory=list)
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

    row: Command              # the command's row, whose usage the handler's usage lines say
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
        summary="what holds your notes together: hubs, bridges and the notes nothing links "
                "to, written to GRAPH_REPORT.md",
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
        usage="<file...> [--target=DIR] [--hub=H] [--profile=P] [--seen=YYYY-MM-DD] [--no-keep-sources] [--anyway]",
        summary="turn files into notes in your vault, one checked write per file; code "
                "becomes outline notes",
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
        summary="turn what keeps coming up in your chats into a note: list it, then keep one",
        examples=("/promote", "/promote user.dog.name"),
        handler="silica.commands.ingress:promote", shlex=True, demo_off=True,
    ),
    Command(
        name="/episodes",
        group="direct",
        usage="[--save=<path>]",
        summary="show what Silica remembers from past chats, dated and grouped; writes "
                "nothing",
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
        summary="show what Silica remembers about you and this vault, and where it is kept",
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
        summary="turn a PDF or another document into a note in the Inbox, word for word",
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
        summary="research a topic on the web and write what it found, cited, as a note in the "
                "Inbox",
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
        name="/council",
        group="workflow",
        usage='"<question>"',
        summary="one question to 2 to 5 models over one search of your notes, then where they "
                "agree and disagree, cited; name the models in Settings, Brains",
        examples=('/council "what do my notes say causes inflation?"',),
        handler="silica.commands.council:council", demo_off=True,
    ),
    Command(
        name="/keep",
        group="direct",
        usage="",
        summary="save the last /web answer as a cited note in the Inbox",
        handler="silica.commands.ingress:keep", demo_off=True,
    ),
    Command(
        name="/fetch",
        group="direct",
        usage="<url>",
        summary="read one web address into a note in the Inbox, word for word: YouTube gives "
                "its transcript, Reddit its thread, GitHub its README, file or issue",
        examples=(
            "/fetch https://arxiv.org/abs/2005.11401",
            "/fetch https://www.youtube.com/watch?v=aircAruvnKk",
            "/fetch https://github.com/psf/requests/issues/6000",
        ),
        handler="silica.commands.ingress:fetch", shlex=True, demo_off=True,
    ),
    Command(
        name="/organize",
        group="workflow",
        usage='"<intent>" [--scope=FOLDER] [--file=taxonomy.yaml] [--merge] [--move-uncategorized] [--apply]',
        summary="sort notes into folders by a scheme you describe",
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
        usage='[note|folder] [--n=10] [--focus="passage"] [--choice]',
        summary="questions on your notes, graded; what you miss comes back later. Name "
                "nothing for what is due; --choice offers four answers to pick from",
        examples=("/quiz Concepts/ML --n=5", "/quiz", "/quiz Concepts/ML --choice"),
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
        name="/page",
        group="workflow",
        usage='"<what>"',
        summary="a web page, or a small site, built from your notes into Pages/; it opens in "
                "a safe frame in the app",
        examples=('/page "a timeline of the Roman republic"', '/page "a three-page site on my thesis chapters"'),
        handler="silica.commands.notes:page", demo_off=True,
    ),
    Command(
        name="/memorize",
        group="workflow",
        usage="[plan] [--n=8] [--plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]] [--teach <note>] [--status [plan]]",
        summary="study a note or a plan from the chat, graded against its source; the "
                "Memorize view does the same on screen",
        examples=("/memorize Concepts/ML", '/memorize "differential forms"'),
        handler="silica.commands.learn:memorize", shlex=True, view="learn", aliases=("/learn",),
    ),
    # Direct — immediate, no LLM round-trip
    Command(
        name="/status",
        group="direct",
        usage="[run_id]",
        summary="what the last long job did, and how far it got",
        handler="silica.commands.vault:status",
    ),
    Command(
        name="/embed",
        group="direct",
        usage="[folder] [--force]",
        summary="build the search index by meaning (the embedding index)",
        handler="silica.commands.index:embed", demo_off=True,
    ),
    Command(
        name="/cooccur",
        group="direct",
        usage="[folder] [--force]",
        summary="build the index of words that appear together, which needs no model",
        handler="silica.commands.index:cooccur",
    ),
    Command(
        name="/lexical",
        group="direct",
        usage="[folder] [--force]",
        summary="build the search index by words",
        handler="silica.commands.index:lexical",
    ),
    Command(
        name="/wiki",
        group="direct",
        usage="[folder|path] [--overview-only] [--force]",
        summary="in a code repository, write ARCHITECTURE.md and one note per part of the "
                "code",
        examples=("/wiki", "/wiki kernel", "/wiki silica/kernel",
                  "/wiki /core/src/main/java/io/github/app/manager",
                  "/wiki --overview-only", "/wiki --force"),
        handler="silica.commands.index:wiki", demo_off=True,
    ),
    Command(
        name="/graph",
        group="direct",
        usage="[out.html] [folder]",
        summary="save the map of your notes and their links as a file",
        handler="silica.commands.index:graph",
    ),
    Command(
        name="/map",
        group="direct",
        usage="<note> [--force]",
        summary="a mind map around one note, saved in maps/ for Obsidian's Canvas",
        handler="silica.commands.index:mind_map",
    ),
    Command(
        name="/find",
        group="direct",
        usage="<query> [--k=N] [--min-trust=human|grounded|distilled|kept] "
              "[--lifecycle=active|review|contested|superseded]",
        summary="search your notes by meaning",
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
        usage="[apply <id…|kind|all> | dismiss <id…|kind>]",
        summary="what Silica proposed and waits on you for: accept it as one change you can "
                "undo, or dismiss it for good",
        examples=("/review", "/review apply autolink", "/review dismiss 3fa9c1d2"),
        handler="silica.commands.review:review",
    ),
    Command(
        name="/anneal",
        group="direct",
        usage="[--steer] [--limit=N]",
        summary="try again the writes set aside because they did not fit; --steer hands what "
                "still fails to a stronger model",
        examples=("/anneal", "/anneal --steer", "/anneal --limit=5"),
        handler="silica.commands.review:anneal", demo_off=True,
    ),
    Command(
        name="/revert",
        group="direct",
        usage="[run-id | --source <file>]",
        summary="take back a whole job's writes: the newest, one that /status names, or "
                "everything made from one source file",
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
        summary="connect other apps and services Silica can use (MCP connectors): list, add, "
                "log in",
        examples=("/connect", "/connect secure github", "/connect login gmail"),
        secret_verbs=("add", "secure"),
        handler="silica.connectors.connect_cli:command", demo_off=True,
    ),
    Command(
        name="/dedup",
        group="direct",
        usage="[folder]",
        summary="find notes that say the same thing and merge them",
        handler="silica.commands.review:dedup", shlex=True, demo_off=True,
    ),
    Command(
        name="/curate",
        group="direct",
        usage="[folder] [--apply]",
        summary="plan a tidy-up of the vault: links to add, lone notes, duplicates; nothing "
                "is written until --apply",
        handler="silica.commands.review:curate", demo_off=True,
    ),
    Command(
        name="/aliases",
        group="direct",
        usage="[folder] [--apply]",
        summary="propose other names a note goes by (abbreviations, spellings); --apply "
                "writes them",
        examples=("/aliases", "/aliases Concepts/AI --apply"),
        handler="silica.commands.review:aliases", demo_off=True,
    ),
    Command(
        name="/enrich",
        group="direct",
        usage="[folder]",
        summary="add what thin notes are missing, as a section at their end",
        handler="silica.commands.review:enrich", shlex=True, demo_off=True,
    ),
    Command(
        name="/stale",
        group="direct",
        usage="[--all] | --stamp [folder] [--write]",
        summary="list notes whose source files changed shape since they were written; --all "
                "counts small edits too, --stamp ties notes to the files they describe",
        handler="silica.commands.reading:stale",
    ),
    Command(
        name="/migrate",
        group="direct",
        usage="[folder] [--write]",
        summary="rewrite notes Silica made in an older format; a list until --write, then one "
                "change you can undo",
        handler="silica.commands.reading:migrate",
    ),
    Command(
        name="/impact",
        group="direct",
        usage="[<git-range>]",
        summary="in a code repository, the notes a code change affects; with no range, the "
                "changes not committed yet",
        handler="silica.commands.reading:impact",
    ),
    Command(
        name="/plans",
        group="direct",
        usage="",
        summary="list your plan notes by status: todo, in progress, blocked, done",
        handler="silica.commands.reading:plans",
    ),
    Command(
        name="/path",
        group="direct",
        usage="<noteA> <noteB>",
        summary="the shortest reading path between two notes, over their links and the ideas "
                "they share",
        examples=('/path "RAG" "Transformers"',),
        handler="silica.commands.reading:path",
    ),
    Command(
        name="/contested",
        group="direct",
        usage="",
        summary="list the open contradictions, each with its age and both sides; the app's note drawer settles them",
        handler="silica.commands.reading:contested",
    ),
    Command(
        name="/contradictions",
        group="direct",
        usage="[folder] [--check]",
        summary="where two notes disagree, as the source checks found it, and nothing "
                "written; --check checks the notes never checked, 40 at a time",
        examples=("/contradictions Projects", "/contradictions Projects --check"),
        handler="silica.commands.contradictions:contradictions", demo_off=True,
    ),
    Command(
        name="/decide",
        group="direct",
        usage="<decision> [--why ...] [--expect ...] [--review YYYY-MM-DD] [--target DIR]",
        summary="write a decision as a note in Decisions/, as typed, no model involved",
        examples=('/decide Move the backups to the NAS --why [[Backup costs]] --expect restores under an hour '
                  '--review 2026-12-15',),
        handler="silica.commands.decisions:decide",
    ),
    Command(
        name="/outcome",
        group="direct",
        usage="<note> <what happened> [--status kept|changed|reversed]",
        summary="add a dated outcome to a decision note",
        examples=('/outcome "Move the backups to the NAS" first restore took 40 minutes --status kept',),
        handler="silica.commands.decisions:outcome",
    ),
    Command(
        name="/decisions",
        group="direct",
        usage="[open|due|all]",
        summary="list decision notes, those due for review first, with what was expected and the last outcome",
        examples=("/decisions", "/decisions due"),
        handler="silica.commands.decisions:listing",
    ),
    Command(
        name="/verify",
        group="direct",
        usage='<note> [--clear] [--as "<name>"]',
        summary="mark a note as checked by you, tied to its current text; --clear takes it "
                "back, --as signs with a name",
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
        summary="see or change this vault's own settings (language, tags)",
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
