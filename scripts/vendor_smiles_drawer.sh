#!/usr/bin/env bash
# Rebuild silica/ui/web/static/smiles-drawer.min.js, which draws a note's
# ```smiles fence as a skeletal formula (ADR-0159), in the app and on a published
# wiki page.
#
# Vendored, not fetched from a CDN, for the reason mermaid is: the web UI loads
# nothing from the network, and a published page runs scripts from its own /_s/
# alone. The file is upstream's own minified build, unchanged, with the licence
# line its minifier dropped put back on top. smiles-drawer is MIT (c) 2017 GDB /
# Reymond Research Group; the build bundles chroma-js (BSD-3-Clause), whose
# notice upstream keeps at the end of the file.
#
# The version is pinned and the tarball checked against the registry's own
# integrity hash, so the file is reproducible: bump the pin, rerun.
set -euo pipefail

version="2.4.1"
out="$(cd "$(dirname "$0")/.." && pwd)/silica/ui/web/static/smiles-drawer.min.js"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

npm pack "smiles-drawer@${version}" --silent >/dev/null
want="$(npm view "smiles-drawer@${version}" dist.integrity)"
got="sha512-$(openssl dgst -sha512 -binary "smiles-drawer-${version}.tgz" | base64 -w0)"
[ "$want" = "$got" ] || { echo "integrity mismatch: $got != $want" >&2; exit 1; }
tar xzf "smiles-drawer-${version}.tgz"

{
  echo "/* smiles-drawer ${version}, MIT (c) 2017 GDB / Reymond Research Group,"
  echo "   https://github.com/reymond-group/smilesDrawer. Upstream dist/smiles-drawer.min.js,"
  echo "   unchanged, vendored by scripts/vendor_smiles_drawer.sh. */"
  # the map is not shipped, so the comment pointing at it goes
  sed '/^\/\/# sourceMappingURL=/d' package/dist/smiles-drawer.min.js
} > "$out"
echo "wrote $out ($(wc -c < "$out") bytes)"
