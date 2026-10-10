#!/usr/bin/env bash
# Rebuild silica/ui/web/static/codemirror.min.js, the one CodeMirror 6 bundle
# the note drawer's live preview (static/app-editor.js) runs on.
#
# Why a vendored bundle and not a CDN import: the web UI is a localhost app
# that loads nothing from the network (mermaid and force-graph are vendored the
# same way), and CodeMirror is a set of ES modules that must share ONE copy of
# @codemirror/state, which only a bundler guarantees. The bundle is upstream
# code only, re-exported on `window.CM`; the live-preview logic stays a plain
# script beside the rest of the app, so editing it needs no build step.
#
# `markdown()` is deliberately NOT exported: it hard-wires @codemirror/lang-html
# (plus css, javascript and autocomplete behind it) for HTML-in-markdown
# highlighting the preview never draws: 507 KB -> 319 KB without them
# (measured 2026-09-28). The
# editor builds its Language from `markdownLanguage.data` instead, which keeps
# the identity `markdownKeymap`'s list continuation checks for.
#
# Versions are pinned here, so the file is reproducible: bump a pin, rerun.
set -euo pipefail

out="$(cd "$(dirname "$0")/.." && pwd)/silica/ui/web/static/codemirror.min.js"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

pkgs=(
  @codemirror/state@6.7.6
  @codemirror/view@6.43.13
  @codemirror/commands@6.11.1
  @codemirror/language@6.12.4
  @codemirror/lang-markdown@6.5.2
  @lezer/markdown@1.7.2
  esbuild@0.28.2
)
npm init -y >/dev/null
npm install --silent --no-audit --no-fund "${pkgs[@]}"

cat > entry.js <<'JS'
export { EditorState, EditorSelection, StateField, StateEffect, RangeSetBuilder, Prec, Transaction } from "@codemirror/state";
export { EditorView, Decoration, WidgetType, keymap, drawSelection, placeholder } from "@codemirror/view";
export { defaultKeymap, history, historyKeymap, indentMore, indentLess } from "@codemirror/commands";
export { Language, LanguageSupport, syntaxTree, ensureSyntaxTree } from "@codemirror/language";
export { markdownLanguage, markdownKeymap } from "@codemirror/lang-markdown";
JS

banner="/* CodeMirror 6 + Lezer, MIT (c) Marijn Haverbeke and others. Built by scripts/vendor_codemirror.sh from: ${pkgs[*]:0:6} */"
npx esbuild entry.js --bundle --minify --format=iife --global-name=CM \
  --target=es2020 --legal-comments=none --banner:js="$banner" --outfile="$out"
echo "wrote $out ($(wc -c < "$out") bytes)"
