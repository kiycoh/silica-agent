// The drawer's editor: Obsidian's Live Preview on CodeMirror 6 (ADR-0051),
// and since ADR-0059 the only way the drawer shows a note.
//
// Loaded with the first note the drawer opens, after static/codemirror.min.js
// (built by scripts/vendor_codemirror.sh), which puts CodeMirror on `window.CM`.
//
// What makes it a live preview and not a source view is one rule, Obsidian's:
// markup is hidden and its effect drawn wherever the selection is NOT, and
// shown as typed wherever it is. An inline element (bold, a link) opens when
// the caret touches it, a line mark (`#`, `>`) when the caret is on its line,
// a block (table, code, math, callout, properties) when the caret enters it.
// Nothing is rendered twice: a block is drawn by the server's own reader
// renderer (POST /note/render), so a table or a formula reads the same in the
// editor as in the reader, and the editor carries no second markdown engine.
//
// ponytail: decorations are rebuilt over the whole document on every change
// and every caret move. Vault notes are a few KB; reopen at 200 KB, where the
// upgrade is building over `view.visibleRanges` in a ViewPlugin for the inline
// part and keeping only the block widgets in this StateField.
(() => {
  const {
    EditorState, StateField, StateEffect, Transaction, Prec,
    EditorView, Decoration, WidgetType, keymap, drawSelection,
    defaultKeymap, history, historyKeymap, indentMore, indentLess,
    Language, LanguageSupport, syntaxTree, ensureSyntaxTree,
    markdownLanguage, markdownKeymap,
  } = window.CM;

  // --- the Obsidian syntax Lezer does not know ------------------------------
  // Frontmatter is not here: it is found by a regex on the document head (see
  // frontmatterEnd), because a block parser that meets an unterminated `---`
  // has already consumed the lines it would have to give back.
  const WIKI = /^!?\[\[([^[\]\n]+?)\]\]/;
  // A tag is `#` plus letters, digits, `_`, `-`, `/`, with at least one
  // non-digit, after a space or at the start: Obsidian's rule, so `#1` and
  // `a#b` stay text.
  const TAG = /^#[\p{L}\p{N}_/-]*[\p{L}_/-][\p{L}\p{N}_/-]*/u;
  // Inline math mirrors the server's dollarmath options (allow_space=False,
  // allow_digits=False), so a `$5 and $10` the reader leaves alone is not a
  // formula here either.
  const MATH = /^\$(?=[^\s$])((?:\\.|[^$\\\n])*?[^\s$])\$(?!\d)/;

  // One object for both ends: Lezer pairs an opening and a closing delimiter
  // by identity, so a fresh literal per call never closes.
  const HIGHLIGHT = { resolve: "Highlight", mark: "HighlightMark" };
  // Footnotes, as the reader's footnote_plugin reads them: a definition is a
  // line opening with `[^label]:`, and it ends the paragraph above it, as
  // markdown-it-footnote's does; a reference is `[^label]` anywhere else.
  // Without the definition parser, `[^L]: [[sources/X]]` was a link
  // reference to the literal `[[sources/X]]`, and nothing in it was drawn.
  const FN_DEF = /^\[\^([^\]\s]+)\]:[ \t]?/;
  const FN_REF = /^\[\^([^\]\s]+)\]/;
  const footnoteDefinition = {
    nextLine: () => false,
    finish(cx, leaf) {
      const m = FN_DEF.exec(leaf.content), at = leaf.start + m[0].length;
      cx.addLeafElement(leaf, cx.elt("FootnoteDefinition", leaf.start, leaf.start + leaf.content.length,
        [cx.elt("FootnoteMark", leaf.start, at), ...cx.parser.parseInline(leaf.content.slice(m[0].length), at)]));
      return true;
    },
  };

  const Obsidian = {
    defineNodes: ["WikiLink", "Embed", "Highlight", "HighlightMark", "HashTag", "InlineMath", "MathBlock",
                  "FootnoteRef", "FootnoteDefinition", "FootnoteMark"],
    parseInline: [
      {
        name: "FootnoteRef",
        before: "Link", // `[^1]` is not a link reference, though it reads like one
        parse(cx, next, pos) {
          if (next !== 91 || cx.char(pos + 1) !== 94) return -1; // `[^`
          const m = FN_REF.exec(cx.slice(pos, cx.end));
          return m ? cx.addElement(cx.elt("FootnoteRef", pos, pos + m[0].length)) : -1;
        },
      },
      {
        name: "WikiLink",
        before: "Link", // `[[` must not reach the link parser, which reads `[x]`
        parse(cx, next, pos) {
          if (next !== 91 && next !== 33) return -1; // `[` or `!`
          const m = WIKI.exec(cx.slice(pos, cx.end));
          if (!m) return -1;
          return cx.addElement(cx.elt(m[0][0] === "!" ? "Embed" : "WikiLink", pos, pos + m[0].length));
        },
      },
      {
        name: "Highlight",
        after: "Emphasis",
        parse(cx, next, pos) {
          if (next !== 61 || cx.char(pos + 1) !== 61 || cx.char(pos + 2) === 61) return -1; // `==`
          const before = cx.slice(pos - 1, pos), after = cx.slice(pos + 2, pos + 3);
          return cx.addDelimiter(HIGHLIGHT, pos, pos + 2,
            !/\s|^$/.test(after), !/\s|^$/.test(before));
        },
      },
      {
        name: "HashTag",
        parse(cx, next, pos) {
          if (next !== 35 || (pos > cx.offset && !/[\s(]/.test(cx.slice(pos - 1, pos)))) return -1;
          const m = TAG.exec(cx.slice(pos, cx.end));
          return m ? cx.addElement(cx.elt("HashTag", pos, pos + m[0].length)) : -1;
        },
      },
      {
        name: "InlineMath",
        parse(cx, next, pos) {
          if (next !== 36 || cx.char(pos + 1) === 36 || /\d/.test(cx.slice(pos - 1, pos))) return -1;
          const m = MATH.exec(cx.slice(pos, cx.end));
          return m ? cx.addElement(cx.elt("InlineMath", pos, pos + m[0].length)) : -1;
        },
      },
    ],
    parseBlock: [{
      name: "FootnoteDefinition",
      before: "LinkReference",
      leaf: (cx, leaf) => (FN_DEF.test(leaf.content) ? footnoteDefinition : null),
      endLeaf: (cx, line) => FN_DEF.test(line.text.slice(line.pos)),
    }, {
      name: "MathBlock",
      before: "FencedCode",
      // `$$` opens a display formula that runs to the next line ending in `$$`,
      // or to the end of its container: the same shape as a code fence, and
      // the same container check FencedCode makes.
      parse(cx, line) {
        const text = line.text.slice(line.pos);
        if (!text.startsWith("$$")) return false;
        const from = cx.lineStart + line.pos;
        const done = (to) => { cx.addElement(cx.elt("MathBlock", from, to)); return true; };
        if (text.length > 4 && /\$\$\s*$/.test(text.slice(2))) {
          const to = cx.lineStart + line.text.length;
          cx.nextLine();
          return done(to);
        }
        while (cx.nextLine() && line.depth >= cx.stack.length) {
          if (/\$\$\s*$/.test(line.text)) {
            const to = cx.lineStart + line.text.length;
            cx.nextLine();
            return done(to);
          }
        }
        return done(cx.prevLineEnd());
      },
    }],
  };

  const language = new Language(markdownLanguage.data, markdownLanguage.parser.configure([Obsidian]), [], "markdown");

  // --- widgets --------------------------------------------------------------
  // Each editor's hooks and heading element live in its own state, not in the
  // module: the drawer and Learn's write view are two editors of one kind
  // (ADR-0134 point 6), and module state let the second create() take the
  // first one's hooks. Every reader has a view or a state in hand.
  const NO_INST = { hooks: {}, top: null };
  const instField = StateField.define({ create: () => NO_INST, update: (v) => v });
  const hooksOf = (view) => view.state.field(instField).hooks;
  const renderCache = new Map(); // source -> html; a block redrawn is not refetched, by either editor

  function rendered(view, source) {
    if (renderCache.has(source)) return Promise.resolve(renderCache.get(source));
    return hooksOf(view).render(source).then((html) => { renderCache.set(source, html); return html; });
  }

  // A link inside something the server drew (a wikilink in a table cell, a
  // URL in a callout) opens; any other press puts the caret at the start of
  // the block, which is what opens it for editing. Click is swallowed too, or
  // the app's delegated `.note-link` handler reopens the note in the reader.
  function followOrReveal(view, el, e) {
    if (e.button !== 0) return;
    const link = e.target.closest(".note-link, a[href]");
    if (link) {
      e.preventDefault();
      if (link.classList.contains("note-link")) { if (link.dataset.path) hooksOf(view).openNote(link.dataset.path); }
      else window.open(link.href, "_blank", "noopener");
      return;
    }
    if (e.target.closest("summary")) return; // the properties box folds, natively
    e.preventDefault();
    view.dispatch({ selection: { anchor: view.posAtDOM(el) } });
    view.focus();
  }

  // `glyphs`: a source check's marks on a table's rows (tableGlyphs), drawn
  // at each row's left edge, the only place a row's finding can show.
  class Rendered extends WidgetType {
    constructor(source, block, glyphs = []) {
      super(); this.source = source; this.block = block; this.glyphs = glyphs; this.gkey = JSON.stringify(glyphs);
    }
    eq(o) { return o.source === this.source && o.block === this.block && o.gkey === this.gkey; }
    toDOM(view) {
      const el = document.createElement(this.block ? "div" : "span");
      el.className = "cm-rendered " + (this.block ? "cm-rendered-block" : "cm-rendered-inline");
      const fill = (html) => {
        el.innerHTML = this.block ? html : html.replace(/^\s*<p>([\s\S]*)<\/p>\s*$/, "$1");
        const hooks = hooksOf(view);
        if (hooks.decorate) hooks.decorate(el, view);
        const rows = el.querySelectorAll("tbody tr");
        for (const [i, grade, hollow, line, n] of this.glyphs) {
          const cell = rows[i] && rows[i].firstElementChild;
          if (cell) { cell.classList.add("cm-chk-cell"); cell.prepend(glyphButton(view, grade, hollow, line, n)); }
        }
        view.requestMeasure();
      };
      if (renderCache.has(this.source)) fill(renderCache.get(this.source));
      else {
        el.textContent = this.source; // the source, dimmed, until the server answers
        el.classList.add("cm-rendering");
        rendered(view, this.source).then((h) => { el.classList.remove("cm-rendering"); fill(h); },
          () => el.classList.remove("cm-rendering")); // unrendered stays readable source
      }
      el.addEventListener("mousedown", (e) => followOrReveal(view, el, e));
      el.addEventListener("click", (e) => { if (!e.target.closest("summary")) { e.preventDefault(); e.stopPropagation(); } });
      return el;
    }
    ignoreEvent() { return true; }
  }

  class Bullet extends WidgetType {
    eq() { return true; }
    toDOM() { const s = document.createElement("span"); s.className = "cm-bullet"; s.textContent = "•"; return s; }
  }

  class Rule extends WidgetType {
    eq() { return true; }
    toDOM() { const s = document.createElement("span"); s.className = "cm-hr"; return s; }
  }

  // The checkbox is the task marker: pressing it rewrites `[ ]` <-> `[x]` in the
  // text, so there is no checkbox state apart from the note's own bytes.
  class Checkbox extends WidgetType {
    constructor(checked) { super(); this.checked = checked; }
    eq(o) { return o.checked === this.checked; }
    toDOM(view) {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "cm-task";
      box.checked = this.checked;
      box.setAttribute("aria-label", this.checked ? "done" : "to do");
      box.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const at = view.posAtDOM(box) + 1;
        view.dispatch({ changes: { from: at, to: at + 1, insert: this.checked ? " " : "x" } });
      });
      box.addEventListener("click", (e) => e.preventDefault()); // the text decides, not the input
      return box;
    }
    ignoreEvent() { return true; }
  }

  // A footnote reference draws as the reader's does, a superscript number in
  // first-reference order, and a press goes to its definition, as the
  // reader's `#fn` anchor scrolls there.
  class FootnoteRef extends WidgetType {
    constructor(n, def) { super(); this.n = n; this.def = def; }
    eq(o) { return o.n === this.n && o.def === this.def; }
    toDOM(view) {
      const s = document.createElement("sup");
      s.className = "footnote-ref";
      s.textContent = String(this.n);
      s.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        view.dispatch({ effects: EditorView.scrollIntoView(this.def, { y: "center" }) });
      });
      return s;
    }
    ignoreEvent() { return true; }
  }

  class FootnoteLabel extends WidgetType {
    constructor(text) { super(); this.text = text; }
    eq(o) { return o.text === this.text; }
    toDOM() { const s = document.createElement("span"); s.className = "cm-fn-label"; s.textContent = this.text; return s; }
  }

  // What heads the note without being its bytes (a paper's strip, ADR-0055):
  // a block before the first line, so it scrolls with the text as it did in
  // the reader, and no keystroke can reach it.
  class Top extends WidgetType {
    constructor(el) { super(); this.el = el; }
    eq(o) { return o.el === this.el; }
    toDOM() { return this.el; }
    ignoreEvent() { return true; }
  }

  const HIDE = Decoration.replace({});
  const BULLET = Decoration.replace({ widget: new Bullet() });
  const RULE = Decoration.replace({ widget: new Rule() });
  const line = (cls) => Decoration.line({ class: cls });
  const LINE = {
    h: [null, 1, 2, 3, 4, 5, 6].map((n) => n && line("cm-h cm-h" + n)),
    quote: line("cm-quote"), fm: line("cm-fm"), code: line("cm-codeblock"),
    table: line("cm-table"), math: line("cm-mathblock"), hr: line("cm-hr-line"),
    fn: line("cm-footnote"),
  };
  const MARK = {
    Emphasis: Decoration.mark({ tagName: "em" }),
    StrongEmphasis: Decoration.mark({ tagName: "strong" }),
    Strikethrough: Decoration.mark({ tagName: "s" }),
    Highlight: Decoration.mark({ tagName: "mark" }),
    InlineCode: Decoration.mark({ tagName: "code" }),
    HashTag: Decoration.mark({ class: "tag" }),
    InlineMath: Decoration.mark({ class: "cm-math-src" }),
    FootnoteRef: Decoration.mark({ class: "cm-fn-src" }),
  };
  const HIDDEN_MARKS = {
    Emphasis: "EmphasisMark", StrongEmphasis: "EmphasisMark", Strikethrough: "StrikethroughMark",
    Highlight: "HighlightMark", InlineCode: "CodeMark",
  };
  // A link is only clickable while it is drawn as one; with the caret in it,
  // it is text you are editing and a press has to place the caret. A wikilink
  // also wears the reader's `note-link`, so the two draw it alike.
  const linkMark = (attrs, live, cls = "") => Decoration.mark({
    tagName: "a", class: (live ? "cm-link cm-link-live " : "cm-link ") + cls, attributes: live ? attrs : {},
  });
  const WIKI_BROKEN = Decoration.mark({ tagName: "a", class: "cm-link note-link broken" });

  // --- the rule -------------------------------------------------------------
  const focusEffect = StateEffect.define();
  // Marks the one transaction that only brings the field in line with the
  // disk (the server's stamped bytes): not an edit, so it neither saves nor
  // lands in the undo history.
  const adoptEffect = StateEffect.define();
  const focused = StateField.define({
    create: () => false,
    update: (v, tr) => tr.effects.reduce((a, e) => (e.is(focusEffect) ? e.value : a), v),
  });
  // The wikilink targets the vault does not resolve. The server answers it
  // (GET /note/raw, and every save), since only its resolver knows the vault;
  // the reader drew these `note-link broken`, and so does this.
  const brokenEffect = StateEffect.define();
  const broken = StateField.define({
    create: () => new Set(),
    update: (v, tr) => tr.effects.reduce((a, e) => (e.is(brokenEffect) ? new Set(e.value) : a), v),
  });
  // The line a chat citation opened the note at (ADR-0117). It lives in the
  // note's state, so the next load() drops it, and edits carry it along.
  const markEffect = StateEffect.define();
  const CITE_LINE = Decoration.line({ class: "cm-cite-line" });
  const marked = StateField.define({
    create: () => Decoration.none,
    update: (v, tr) => tr.effects.reduce((a, e) => (e.is(markEffect) ? e.value : a), v.map(tr.changes)),
    provide: (f) => EditorView.decorations.from(f),
  });
  const linkKey = (target) => target.split("#")[0].trim();

  // --- a source check's marks (ADR-0110 point 11) ----------------------------
  // The findings stay in the lines of the text the check read (`base`), and
  // every change since is composed onto one ChangeSet (`since`): a mark is
  // mapped through it when drawn, and a block it touched is hollow, "changed
  // since the check", so no mark vouches for text the check never read.
  const checkReset = StateEffect.define();   // {base, findings, states} | null
  const checkAdd = StateEffect.define();     // {findings, states}, as the model's arrive
  const checkReplace = StateEffect.define(); // {findings, states}: the rerun after an edit
  const checkOpen = StateEffect.define();    // the open box's block (its line in `base`), or 0
  const checkShow = StateEffect.define();    // Set of the grades, and "not found", on screen
  const checkField = StateField.define({
    create: () => ({ base: null, since: null, findings: [], states: [], open: 0, show: null }),
    update(v, tr) {
      let o = v;
      if (tr.docChanged && o.base) o = { ...o, since: o.since ? o.since.compose(tr.changes) : tr.changes };
      for (const e of tr.effects) {
        if (e.is(checkReset)) {
          o = e.value ? { ...o, ...e.value, since: null } : { ...o, base: null, since: null, findings: [], states: [], open: 0 };
        } else if (e.is(checkAdd)) {
          o = { ...o, findings: o.findings.concat(e.value.findings), states: o.states.concat(e.value.states) };
        } else if (e.is(checkReplace)) o = { ...o, ...e.value };
        else if (e.is(checkOpen)) o = { ...o, open: e.value };
        else if (e.is(checkShow)) o = { ...o, show: e.value };
      }
      return o;
    },
  });
  const RANK = { error: 0, warning: 1, nit: 2 };
  // The shape differs by grade and fills only while the mark is current, so
  // neither colour nor fill is the only signal.
  const GLYPH = { error: ["■", "□"], warning: ["▲", "△"], nit: ["●", "○"] };
  const ROW = /^\s*\|/;
  // A list, quote or footnote marker is not what a block says.
  const LEAD = /^\s*(?:(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?|(?:>\s?)+|\[\^[^\]\s]+\]:\s?)/;

  // The last check's blocks as they stand in this text: {line (in base), from,
  // to, hollow, edited, row, findings (on screen), states}. `edited`: touched
  // since the check or its last rerun; `hollow` also counts what a rerun has
  // already carried over hollow.
  function checkBlocks(state, all = false) {
    const c = state.field(checkField);
    if (!c.base) return [];
    const doc = state.doc, by = new Map();
    const shown = (f) => all || !c.show || (c.show.has(f.grade) && (f.kind !== "not found" || c.show.has("not found")));
    const at = (x) => {
      if (!by.has(x.line)) by.set(x.line, { line: x.line, end: x.end || x.line, findings: [], states: [] });
      return by.get(x.line);
    };
    for (const f of c.findings) if (shown(f)) at(f).findings.push(f);
    for (const s of c.states) at(s).states.push(s);
    const out = [];
    for (const b of by.values()) {
      if (b.end > c.base.lines) continue;
      const a = c.base.line(b.line).from, z = c.base.line(b.end).to;
      const from = c.since ? c.since.mapPos(a, 1) : a, to = c.since ? c.since.mapPos(z, -1) : z;
      if (to < from) continue; // the block was deleted
      const edited = !!c.since && c.since.touchesRange(a, z);
      // The glyph is hollow when every mark under it is; a rerun's fresh
      // rule finding beside a carried-over verdict keeps it filled.
      out.push({ ...b, from, to, edited, hollow: edited || (b.findings.length > 0 && b.findings.every((f) => f.hollow)),
                 row: ROW.test(doc.lineAt(from).text) });
    }
    return out.sort((x, y) => x.from - y.from);
  }

  const gradeOf = (b) => b.findings.reduce((g, f) => (RANK[f.grade] < RANK[g] ? f.grade : g), "nit");

  // The words a finding is about, found in its block with any run of spaces
  // matching any other (a date wrapped over two lines); the block's text,
  // past its marker, when they are not there or the finding is the model's.
  function findingRange(doc, b, f) {
    const text = doc.sliceString(b.from, b.to), want = (f.mark || f.words || "").trim();
    if (want && !f.model) {
      const rx = new RegExp(want.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+"), "i");
      const m = rx.exec(text);
      if (m) return [b.from + m.index, b.from + m.index + m[0].length];
    }
    const lead = LEAD.exec(text);
    return [b.from + (lead ? lead[0].length : 0), b.to];
  }

  function toggleBox(view, line) {
    const open = view.state.field(checkField).open;
    view.dispatch({ effects: checkOpen.of(open === line ? 0 : line) });
  }

  class CheckGlyph extends WidgetType {
    constructor(grade, hollow, line, n) { super(); this.grade = grade; this.hollow = hollow; this.line = line; this.n = n; }
    eq(o) { return o.grade === this.grade && o.hollow === this.hollow && o.line === this.line && o.n === this.n; }
    toDOM(view) { return glyphButton(view, this.grade, this.hollow, this.line, this.n); }
    ignoreEvent() { return true; }
  }

  function glyphButton(view, grade, hollow, line, n) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `cm-chk-glyph cm-chk-${grade}` + (hollow ? " cm-chk-hollow" : "");
    b.textContent = GLYPH[grade][hollow ? 1 : 0];
    b.setAttribute("aria-label", `${grade}, ${n} finding${n > 1 ? "s" : ""}` +
      (hollow ? ", changed since the check" : "") + ": open");
    b.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation(); // in a table, the rendered block would take it as a caret
      toggleBox(view, line);
    });
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); if (e.detail === 0) toggleBox(view, line); });
    return b;
  }

  // One block's box: every finding with its grade, kind, sentence and the
  // passage that decided it, then the states that carry arithmetic.
  class FindingBox extends WidgetType {
    constructor(b) {
      super();
      this.b = b;
      this.key = JSON.stringify([b.line, b.edited, b.findings, b.states]);
    }
    eq(o) { return o.key === this.key; }
    toDOM(view) {
      const b = this.b, box = document.createElement("div");
      box.className = "cm-chk-box";
      box.setAttribute("role", "note");
      const close = document.createElement("button");
      close.type = "button";
      close.className = "cm-chk-close";
      close.setAttribute("aria-label", "close · Esc");
      close.textContent = "×";
      close.addEventListener("mousedown", (e) => { e.preventDefault(); view.dispatch({ effects: checkOpen.of(0) }); });
      box.append(close);
      const stale = (f) => b.edited || !!f.hollow;
      if (b.findings.some(stale)) {
        const p = document.createElement("p");
        p.className = "cm-chk-stale";
        p.textContent = "changed since the check: these marks read the text as it was";
        box.append(p);
      }
      const findings = [...b.findings].sort((x, y) => RANK[x.grade] - RANK[y.grade]);
      for (const f of findings) {
        const row = document.createElement("div");
        row.className = `cm-chk-f cm-chk-${f.grade}`;
        const head = document.createElement("div");
        head.className = "cm-chk-head";
        head.textContent = `${GLYPH[f.grade][stale(f) ? 1 : 0]} ${f.grade} · ${f.kind}`;
        const say = document.createElement("p");
        say.textContent = f.say;
        row.append(head, say);
        if (f.passage) {
          const q = document.createElement("blockquote");
          q.textContent = f.passage;
          row.append(q);
        }
        const m = /^(.+?\.md)(?::(\d+))?$/.exec(f.at || "");
        if (m) {
          const a = document.createElement("button");
          a.type = "button";
          a.className = "cm-chk-src";
          a.textContent = f.at;
          a.addEventListener("mousedown", (e) => { e.preventDefault(); hooksOf(view).openAt(m[1], m[2] ? +m[2] : 0); });
          row.append(a);
        }
        box.append(row);
      }
      for (const s of b.states) {
        if (s.state === "supported" || s.state === "not a statement of fact") continue;
        const p = document.createElement("p");
        p.className = "cm-chk-state";
        p.textContent = `${s.state}: ${s.say}`;
        box.append(p);
      }
      return box;
    }
    ignoreEvent() { return true; }
  }

  // The marks, beside the live preview's: an underline per finding (the
  // highest grade wins where two meet), a glyph at the block's left edge,
  // and the one open box under its block. A table row has its glyph alone:
  // its line is drawn as a rendered table, where no underline would show,
  // so the row's glyph is drawn into the table (buildDecorations).
  function buildChecks(state) {
    const doc = state.doc, c = state.field(checkField), out = [];
    for (const b of checkBlocks(state)) {
      if (b.findings.length) {
        const grade = gradeOf(b);
        out.push(Decoration.widget({ widget: new CheckGlyph(grade, b.hollow, b.line, b.findings.length), side: -1 })
          .range(doc.lineAt(b.from).from));
        if (!b.row) {
          const taken = [];
          for (const f of [...b.findings].sort((x, y) => RANK[x.grade] - RANK[y.grade])) {
            let pieces = [findingRange(doc, b, f)];
            for (const [ta, tb] of taken) {
              pieces = pieces.flatMap(([pa, pb]) => (pb <= ta || pa >= tb ? [[pa, pb]]
                : [[pa, ta], [tb, pb]].filter(([x, y]) => y > x)));
            }
            taken.push(findingRange(doc, b, f));
            const deco = Decoration.mark({
              class: `cm-chk-u cm-chk-${f.grade}` + (b.edited || f.hollow ? " cm-chk-hollow" : ""),
              attributes: { "data-chk": String(b.line) },
            });
            for (const [pa, pb] of pieces) if (pb > pa) out.push(deco.range(pa, pb));
          }
        }
      }
      if (c.open === b.line && (b.findings.length || b.states.length)) {
        let end = doc.lineAt(b.to);
        if (b.row) while (end.number < doc.lines && ROW.test(doc.line(end.number + 1).text)) end = doc.line(end.number + 1);
        out.push(Decoration.widget({ widget: new FindingBox(b), block: true, side: 1 }).range(end.to));
      }
    }
    return Decoration.set(out, true);
  }

  const checkDecos = EditorView.decorations.compute(["doc", checkField], buildChecks);

  // An underline opens its block's box; the press still places the caret.
  const checkPress = EditorView.domEventHandlers({
    mousedown(e, view) {
      const u = e.target.closest && e.target.closest(".cm-chk-u");
      if (u && e.button === 0) view.dispatch({ effects: checkOpen.of(+u.dataset.chk) });
      return false;
    },
  });

  // The glyphs of a rendered table's rows, as [tbody index, grade, hollow,
  // block line, count]: what the table's widget draws and compares on.
  function tableGlyphs(state, from, to) {
    const first = state.doc.lineAt(from).number;
    return checkBlocks(state).filter((b) => b.row && b.findings.length && b.from >= from && b.from <= to)
      .map((b) => [state.doc.lineAt(b.from).number - first - 2, gradeOf(b), b.hollow, b.line, b.findings.length]);
  }

  const FRONTMATTER = /^---[ \t]*\n(?:[\s\S]*?\n)?(?:---|\.\.\.)[ \t]*(?=\n|$)/;
  // Obsidian's frontmatter only counts at byte 0 and only when it closes.
  // `head` is the document's first 64 KB, which no properties block outgrows.
  function frontmatterEnd(head) {
    const m = FRONTMATTER.exec(head);
    return m ? m[0].length : -1;
  }
  const HEAD = 65536;

  function buildDecorations(state) {
    const doc = state.doc;
    const tree = ensureSyntaxTree(state, doc.length, 200) || syntaxTree(state);
    // Unfocused, nothing is open: the note reads as rendered while you are
    // in the chat or the note's menu, and opens again where you left it.
    const sel = state.field(focused) ? state.selection.ranges : [];
    const touches = (from, to) => sel.some((r) => r.from <= to && r.to >= from);
    const lineTouched = (pos) => { const l = doc.lineAt(pos); return touches(l.from, l.to); };
    const out = [];
    const add = (deco, from, to = from) => out.push(deco.range(from, to));
    const hide = (from, to) => { if (to > from) add(HIDE, from, to); };
    const eachLine = (from, to, deco) => {
      for (let p = from; p <= to;) { const l = doc.lineAt(p); add(deco, l.from); p = l.to + 1; }
    };
    // A block widget has to own whole lines. One that starts after a `> ` or
    // a list indent that is not blank is left as styled source instead.
    const blockOrLines = (from, to, deco) => {
      const a = doc.lineAt(from), b = doc.lineAt(to);
      const whole = !doc.sliceString(a.from, from).trim() && !doc.sliceString(to, b.to).trim();
      if (whole && !touches(a.from, b.to)) {
        const glyphs = deco === LINE.table ? tableGlyphs(state, a.from, b.to) : [];
        add(Decoration.replace({ widget: new Rendered(doc.sliceString(a.from, b.to), true, glyphs), block: true }), a.from, b.to);
      } else {
        eachLine(from, to, deco);
      }
    };

    const missing = state.field(broken);
    const refs = [], defs = []; // footnotes, numbered once the whole tree is seen
    const top = state.field(instField).top; // the element heading the note, set per load()
    if (top) add(Decoration.widget({ widget: new Top(top), block: true, side: -1 }), 0);
    const fmEnd = frontmatterEnd(doc.sliceString(0, HEAD));
    if (fmEnd > 0) blockOrLines(0, fmEnd, LINE.fm);

    tree.iterate({
      enter(n) {
        const name = n.name, node = n.node;
        if (name === "Document") return;
        if (n.from < fmEnd) return false;
        const live = !touches(n.from, n.to);
        if (name.startsWith("ATXHeading")) {
          add(LINE.h[+name.slice(-1)], doc.lineAt(n.from).from);
          if (!lineTouched(n.from)) {
            for (let c = node.firstChild; c; c = c.nextSibling) {
              if (c.name !== "HeaderMark") continue;
              if (c.from === n.from) hide(c.from, Math.min(c.to + 1, n.to)); // `# ` with its space
              else hide(doc.sliceString(c.from - 1, c.from) === " " ? c.from - 1 : c.from, c.to); // closing `##`
            }
          }
          return;
        }
        if (name.startsWith("SetextHeading")) {
          const under = node.getChild("HeaderMark");
          eachLine(n.from, under ? under.from - 1 : n.to, LINE.h[+name.slice(-1)]);
          if (under && live) hide(under.from, under.to);
          return;
        }
        if (MARK[name] && HIDDEN_MARKS[name] !== undefined) {
          add(MARK[name], n.from, n.to);
          if (live) {
            for (let c = node.firstChild; c; c = c.nextSibling) if (c.name === HIDDEN_MARKS[name]) hide(c.from, c.to);
          }
          return;
        }
        switch (name) {
          case "HashTag":
            add(MARK.HashTag, n.from, n.to);
            return false;
          case "Escape":
            if (live) hide(n.from, n.from + 1); // `\*` reads as `*`
            return false;
          case "Link": {
            const marks = node.getChildren("LinkMark"), url = node.getChild("URL");
            if (!url || marks.length < 2) return; // `[x][ref]` and `[x]` stay text
            const textFrom = marks[0].to, textTo = marks[1].from;
            if (textTo > textFrom) add(linkMark({ "data-href": doc.sliceString(url.from, url.to) }, live), textFrom, textTo);
            if (live) { hide(n.from, textFrom); hide(textTo, n.to); }
            return;
          }
          case "URL": {
            const parent = node.parent && node.parent.name;
            if (parent === "Link" || parent === "Image" || parent === "LinkReference") return false;
            add(linkMark({ "data-href": doc.sliceString(n.from, n.to) }, live), n.from, n.to);
            return false;
          }
          case "Autolink":
            if (live) for (const m of node.getChildren("LinkMark")) hide(m.from, m.to); // the `<` `>`
            return;
          case "WikiLink": {
            const inner = doc.sliceString(n.from + 2, n.to - 2), bar = inner.indexOf("|");
            const target = (bar < 0 ? inner : inner.slice(0, bar)).trim();
            const shownFrom = bar < 0 ? n.from + 2 : n.from + 2 + bar + 1;
            // A broken link is never live: there is nothing to open, so a
            // press places the caret, as in any other text.
            const dead = missing.has(linkKey(target));
            if (n.to - 2 > shownFrom) {
              add(dead ? WIKI_BROKEN : linkMark({ "data-target": target }, live, "note-link"), shownFrom, n.to - 2);
            }
            if (live) { hide(n.from, shownFrom); hide(n.to - 2, n.to); }
            return false;
          }
          case "FootnoteRef":
            refs.push([n.from, n.to, live]);
            return false;
          case "FootnoteDefinition": {
            const mark = node.getChild("FootnoteMark");
            defs.push([mark.from, mark.to, !lineTouched(n.from)]);
            eachLine(n.from, n.to, LINE.fn);
            return;
          }
          case "FootnoteMark":
            return false;
          // A comment is never prose: the reader's renderer drops it, so the
          // drawer showed `<!-- silica: valid_from=… -->` as a line of text
          // that no other surface shows (2026-10-26). Whole lines go whole, or
          // an empty line would stand where the stamp was.
          case "CommentBlock": {
            const a = doc.lineAt(n.from), b = doc.lineAt(Math.max(n.from, n.to - 1));
            if (!touches(a.from, b.to)) add(Decoration.replace({ block: true }), a.from, b.to);
            return false;
          }
          case "Comment":
            if (live) hide(n.from, n.to);
            return false;
          case "Embed":
          case "Image":
          case "InlineMath":
            if (live) add(Decoration.replace({ widget: new Rendered(doc.sliceString(n.from, n.to), false) }), n.from, n.to);
            else if (name === "InlineMath") add(MARK.InlineMath, n.from, n.to);
            return false;
          case "ListMark": {
            const item = node.parent, list = item && item.parent;
            const task = item && item.getChild("Task"), marker = task && task.getChild("TaskMarker");
            if (marker) {
              // `- [ ] ` draws as one checkbox: the bullet goes, the marker is the box.
              if (!touches(n.from, marker.to)) {
                hide(n.from, marker.from);
                const done = doc.sliceString(marker.from + 1, marker.to - 1).toLowerCase() === "x";
                add(Decoration.replace({ widget: new Checkbox(done) }), marker.from, marker.to);
              }
            } else if (list && list.name === "BulletList" && live) {
              add(BULLET, n.from, n.to);
            }
            return false;
          }
          case "QuoteMark":
            if (!lineTouched(n.from)) hide(n.from, doc.sliceString(n.to, n.to + 1) === " " ? n.to + 1 : n.to);
            return false;
          case "Blockquote": {
            const first = doc.lineAt(n.from);
            if (/^\s*>\s*\[![\w-]+\]/.test(first.text)) { blockOrLines(n.from, n.to, LINE.quote); return false; }
            eachLine(n.from, n.to, LINE.quote);
            return;
          }
          case "HorizontalRule":
            add(LINE.hr, doc.lineAt(n.from).from);
            if (!lineTouched(n.from)) add(RULE, n.from, n.to);
            return false;
          case "FencedCode":
            blockOrLines(n.from, n.to, LINE.code);
            return false;
          case "CodeBlock":
            eachLine(n.from, n.to, LINE.code);
            return false;
          case "Table":
            blockOrLines(n.from, n.to, LINE.table);
            return false;
          case "MathBlock":
            blockOrLines(n.from, n.to, LINE.math);
            return false;
          case "HTMLBlock":
            blockOrLines(n.from, n.to, LINE.code);
            return false;
        }
      },
    });
    // markdown-it numbers a footnote by its first reference and drops a
    // reference with no definition back to text; the same here.
    const label = (from, to) => FN_REF.exec(doc.sliceString(from, to))[1];
    const defAt = new Map(defs.map(([from, to]) => [label(from, to), from]));
    const num = new Map();
    for (const [from, to, live] of refs) {
      const key = label(from, to);
      if (!defAt.has(key)) continue;
      if (!num.has(key)) num.set(key, num.size + 1);
      if (live) add(Decoration.replace({ widget: new FootnoteRef(num.get(key), defAt.get(key)) }), from, to);
      else add(MARK.FootnoteRef, from, to);
    }
    for (const [from, to, live] of defs) {
      const key = label(from, to);
      if (live) add(Decoration.replace({ widget: new FootnoteLabel(String(num.get(key) || key)) }), from, to);
    }
    return Decoration.set(out, true);
  }

  const livePreview = StateField.define({
    create: buildDecorations,
    update(decos, tr) {
      if (tr.docChanged || tr.selection || tr.effects.some((e) => e.is(focusEffect) || e.is(brokenEffect)) ||
          tr.state.field(checkField) !== tr.startState.field(checkField) ||
          syntaxTree(tr.state) !== syntaxTree(tr.startState)) return buildDecorations(tr.state);
      return decos;
    },
    provide: (f) => EditorView.decorations.from(f),
  });

  // --- keys -----------------------------------------------------------------
  // Mod-B / Mod-I wrap the selection, or unwrap it when it is already wrapped;
  // on a bare caret they leave it between the two marks, as Obsidian does.
  function toggleWrap(mark) {
    return (view) => {
      const s = view.state, n = mark.length;
      view.dispatch(s.changeByRange((r) => {
        const text = s.sliceDoc(r.from, r.to);
        if (s.sliceDoc(r.from - n, r.from) === mark && s.sliceDoc(r.to, r.to + n) === mark) {
          return { changes: [{ from: r.from - n, to: r.from }, { from: r.to, to: r.to + n }],
                   range: CM.EditorSelection.range(r.from - n, r.to - n) };
        }
        if (text.length > 2 * n && text.startsWith(mark) && text.endsWith(mark)) {
          return { changes: { from: r.from, to: r.to, insert: text.slice(n, -n) },
                   range: CM.EditorSelection.range(r.from, r.to - 2 * n) };
        }
        return { changes: [{ from: r.from, insert: mark }, { from: r.to, insert: mark }],
                 range: CM.EditorSelection.range(r.from + n, r.to + n) };
      }));
      return true;
    };
  }

  // Tab indents a list item, the one place Obsidian needs it. Anywhere else it
  // is not bound, so it still moves focus out of the editor: an editor that
  // eats Tab everywhere is a keyboard trap (WCAG 2.1.2).
  const inList = (state) => {
    for (let n = syntaxTree(state).resolveInner(state.selection.main.head, -1); n; n = n.parent) {
      if (n.name === "ListItem") return true;
    }
    return false;
  };

  function keys() {
    return [
      { key: "Mod-s", run: (v) => { hooksOf(v).save(); return true; } },
      { key: "Mod-b", run: toggleWrap("**") },
      { key: "Mod-i", run: toggleWrap("*") },
      { key: "Tab", run: (v) => inList(v.state) && indentMore(v) },
      { key: "Shift-Tab", run: (v) => inList(v.state) && indentLess(v) },
      ...markdownKeymap, // Enter continues a list or a quote, Backspace undoes the markup
    ];
  }

  // A drawn link opens on press; with the caret inside it, it is text.
  const linkPress = EditorView.domEventHandlers({
    mousedown(e, view) {
      const a = e.target.closest && e.target.closest("a.cm-link-live");
      if (!a || e.button !== 0) return false;
      e.preventDefault();
      if (a.dataset.target) hooksOf(view).openNote(a.dataset.target);
      else if (a.dataset.href) {
        const href = a.dataset.href;
        if (/^[a-z][\w+.-]*:/i.test(href)) window.open(href, "_blank", "noopener");
        else hooksOf(view).openNote(decodeURIComponent(href)); // `[x](Other note.md)` is a note
      }
      return true;
    },
  });

  // A tree row dropped on the note writes the wikilinks the composer writes
  // (ADR-0126 point 6). Taken before CodeMirror's own drop, which reads the
  // transfer's text and would insert the row's address: a dragged link
  // carries its URL as text. A folder has no token here and shows no drop.
  const ROW_DRAG = "application/x-silica-ws";     // app-workspaces.js WS_DRAG
  const ROW_NOTES = "application/x-silica-notes"; // its NOTE_DRAG: present when notes ride along
  const rowDrop = EditorView.domEventHandlers({
    dragover(e) {
      if (!e.dataTransfer.types.includes(ROW_DRAG)) return false;
      e.preventDefault();
      e.dataTransfer.dropEffect = e.dataTransfer.types.includes(ROW_NOTES) ? "copy" : "none";
      return true;
    },
    drop(e, view) {
      if (!e.dataTransfer.types.includes(ROW_DRAG)) return false;
      e.preventDefault();
      let it = null;
      try { it = JSON.parse(e.dataTransfer.getData(ROW_DRAG) || "null"); } catch { /* not ours: nothing lands */ }
      const notes = (it && Array.isArray(it.notes)) ? it.notes : [];
      const hooks = hooksOf(view);
      if (!notes.length || !hooks.links) return true;
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY }) ?? view.state.selection.main.head;
      const text = hooks.links(notes).join(" ") + " ";
      view.dispatch({ changes: { from: pos, insert: text }, selection: { anchor: pos + text.length } });
      view.focus();
      return true;
    },
  });

  // `inst`, {hooks, top}, is the editor's own; without it (the tests' states)
  // there are no hooks and nothing heads the note.
  function extensions(inst = NO_INST) {
    return [
      instField.init(() => inst),
      history(),
      drawSelection(),
      EditorView.lineWrapping,
      new LanguageSupport(language),
      focused,
      broken,
      marked,
      checkField,
      checkDecos,
      checkPress,
      EditorView.focusChangeEffect.of((_s, on) => focusEffect.of(on)),
      livePreview,
      linkPress,
      rowDrop,
      Prec.high(keymap.of(keys())),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      EditorView.contentAttributes.of({ spellcheck: "true", "aria-label": "note text" }),
      EditorView.updateListener.of((u) => {
        const hooks = hooksOf(u.view);
        if (u.docChanged && !u.transactions.some((t) => t.effects.some((e) => e.is(adoptEffect)))) hooks.change();
        if (u.focusChanged && !u.view.hasFocus) hooks.blur();
        if ((u.selectionSet || u.focusChanged) && hooks.select) hooks.select(u.view);
      }),
    ];
  }

  // hooks: render(source) -> Promise<html>, openNote(target), change(), blur(),
  // save(), decorate(el, view), select(view), links(paths) -> ["[[…]]"],
  // openAt(path, line). The only caller is app-notes.js, once per editor.
  function create(parent, hooks) {
    const inst = { hooks, top: null };
    const view = new EditorView({ parent, state: EditorState.create({ doc: "", extensions: extensions(inst) }) });
    return {
      view,
      getText: () => view.state.doc.toString(),
      // A fresh state per note: the undo history of one note must not reach
      // into the next. The caret starts under the properties, on the text;
      // it does not take focus, which only a click in the text gives it
      // (ADR-0059). `top` is what heads the note, `missing` its broken links.
      load(text, { top = null, missing = [] } = {}) {
        inst.top = top;
        const fm = frontmatterEnd(text.slice(0, HEAD));
        const at = fm > 0 ? Math.min(fm + 1, text.length) : 0;
        const state = EditorState.create({ doc: text, selection: { anchor: at }, extensions: extensions(inst) });
        view.setState(state.update({ effects: brokenEffect.of(missing) }).state);
        view.scrollDOM.scrollTop = 0;
      },
      setMissing: (missing) => view.dispatch({ effects: brokenEffect.of(missing) }),
      // The disk's bytes differ from the field's by what the server added (a
      // `type:` stamp), or by what an agent or /undo wrote under an unchanged
      // field: apply that difference as one change, so the caret and the
      // scroll are mapped through it instead of reset.
      adopt(text) {
        const cur = view.state.doc.toString();
        if (cur === text) return;
        let a = 0;
        while (a < cur.length && a < text.length && cur[a] === text[a]) a++;
        let b = 0;
        while (b < cur.length - a && b < text.length - a && cur[cur.length - 1 - b] === text[text.length - 1 - b]) b++;
        view.dispatch({
          changes: { from: a, to: cur.length - b, insert: text.slice(a, text.length - b) },
          annotations: Transaction.addToHistory.of(false),
          effects: adoptEffect.of(null),
        });
      },
      focus: () => view.focus(),
      // The source check (ADR-0110 point 11). startCheck pins the text the
      // check reads, so it goes after the save and before the request.
      startCheck: () => view.dispatch({ effects: checkReset.of({ base: view.state.doc, findings: [], states: [], open: 0 }) }),
      addCheck: (findings, states) => view.dispatch({ effects: checkAdd.of({ findings, states }) }),
      endCheck: () => view.dispatch({ effects: checkReset.of(null) }),
      showCheck: (show) => view.dispatch({ effects: checkShow.of(show) }),
      hasCheck: () => !!view.state.field(checkField).base,
      closeCheckBox() {
        if (!view.state.field(checkField).open) return false;
        view.dispatch({ effects: checkOpen.of(0) });
        return true;
      },
      // Typed into since the check or its last rerun, and the caret is out of
      // every block typed into (or out of the field): when the rules run
      // again (Open 4), so a half-typed identifier is never flagged.
      checkEdited() {
        const c = view.state.field(checkField);
        if (!c.base || !c.since || c.since.empty) return false;
        if (!view.hasFocus) return true;
        const doc = view.state.doc, head = view.state.selection.main.head;
        let inside = false;
        c.since.iterChangedRanges((_fa, _ta, fb, tb) => {
          let a = doc.lineAt(fb).number, z = doc.lineAt(tb).number;
          while (a > 1 && doc.line(a - 1).text.trim()) a--;
          while (z < doc.lines && doc.line(z + 1).text.trim()) z++;
          if (head >= doc.line(a).from && head <= doc.line(z).to) inside = true;
        });
        return !inside;
      },
      // Before a rerun: the check moves onto the text as it is now, each mark
      // re-lined, an edited block's marks hollow. Returns the model's
      // verdicts now hollow, which the rerun carries until the next check.
      rebaseCheck() {
        const state = view.state, doc = state.doc, c = state.field(checkField);
        if (!c.base) return [];
        const findings = [], states = [];
        let open = 0;
        for (const b of checkBlocks(state, true)) {
          const line = doc.lineAt(b.from).number, end = doc.lineAt(b.to).number;
          for (const f of b.findings) findings.push({ ...f, line, end, hollow: !!f.hollow || b.edited });
          for (const x of b.states) states.push({ ...x, line, end });
          if (c.open === b.line) open = line;
        }
        view.dispatch({ effects: checkReset.of({ base: doc, findings, states, open }) });
        return findings.filter((f) => f.model && f.hollow);
      },
      // A rerun's report replaces the rules' marks; `carried` are rebaseCheck's
      // hollow verdicts on the blocks the report could not judge from the cache.
      replaceCheck(report, carried) {
        const changed = new Set(report.states.filter((x) => x.state === "not checked").map((x) => x.line));
        view.dispatch({ effects: checkReplace.of({
          findings: report.findings.concat(carried.filter((f) => changed.has(f.line))), states: report.states }) });
      },
      // The caret to line n, scrolled to the middle; a line past the end (the
      // note changed since the agenda read it) is left alone.
      revealLine(n) {
        const doc = view.state.doc;
        if (!(n >= 1 && n <= doc.lines)) return;
        const pos = doc.line(n).from;
        view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: "center" }) });
      },
      // revealLine, with the line marked as the one a citation stands on.
      markLine(n) {
        const doc = view.state.doc;
        if (!(n >= 1 && n <= doc.lines)) return;
        const pos = doc.line(n).from;
        view.dispatch({
          selection: { anchor: pos },
          effects: [markEffect.of(Decoration.set([CITE_LINE.range(pos)])), EditorView.scrollIntoView(pos, { y: "center" })],
        });
        // A line inside a rendered block (a callout, a table) has no row of its
        // own on screen, and the field takes no focus to unfold it (ADR-0059),
        // so the block wears the mark until it is drawn again.
        view.requestMeasure({
          read: () => [...view.contentDOM.querySelectorAll(".cm-rendered-block")]
            .filter((el) => { const b = view.lineBlockAt(view.posAtDOM(el)); return b.from <= pos && pos <= b.to; }),
          write: (hit) => {
            for (const el of view.contentDOM.querySelectorAll(".cm-rendered-block.cm-cite-line")) el.classList.remove("cm-cite-line");
            for (const el of hit) el.classList.add("cm-cite-line");
          },
        });
      },
    };
  }

  window.LiveEditor = { create, extensions, buildDecorations, frontmatterEnd, language, focusEffect, brokenEffect };
})();
