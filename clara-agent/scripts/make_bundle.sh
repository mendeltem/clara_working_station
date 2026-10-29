#!/usr/bin/env bash
# Pack the repository as a git bundle (full history) and a zip of the working tree.
# You push from your own machine; no token ever leaves it:
#   git clone clara-agent.bundle clara-agent
#   cd clara-agent && git remote set-url origin https://github.com/mendeltem/clara-agent && git push -u origin main
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="${1:-$ROOT/..}"
cd "$ROOT"
git bundle create "$OUT/clara-agent.bundle" --all
git bundle verify "$OUT/clara-agent.bundle"
rm -f "$OUT/clara-agent.zip"
git archive --format=zip --prefix=clara-agent/ -o "$OUT/clara-agent.zip" HEAD
ls -la "$OUT/clara-agent.bundle" "$OUT/clara-agent.zip"
