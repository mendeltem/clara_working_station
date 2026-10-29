#!/usr/bin/env bash
# "Installable" means: fresh clone, new environment, only the README commands.
# Runs the README blocks verbatim in a throwaway folder and venv, then the tests.
#
#   scripts/fresh_install_test.sh                 # from a checkout: clones this folder's git repo
#   scripts/fresh_install_test.sh https://github.com/mendeltem/clara-agent
set -eu
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK=$(mktemp -d -t clara-fresh-XXXXXX)
echo "fresh install test in $WORK (source: $SRC)"
cd "$WORK"
git clone --quiet "$SRC" clara-agent
cd clara-agent
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check .
"$WORK/venv/bin/clara" --version
export CLARA_HOME="$WORK/home"
"$WORK/venv/bin/clara" doctor --url http://127.0.0.1:9/v1 && { echo "doctor should have reported a problem" >&2; exit 1; } || true
"$WORK/venv/bin/clara" --mock -p "show me the project" >/dev/null
"$WORK/venv/bin/clara" sandbox-check || echo "sandbox-check reported a problem (expected only on kernels without Landlock)"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check pytest
"$WORK/venv/bin/python" -m pytest -q
echo "fresh install test passed in $WORK"
