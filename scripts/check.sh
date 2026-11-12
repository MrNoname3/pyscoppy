#!/usr/bin/env bash
# Release gate — the single entry point for every repo check.
#
#   ./scripts/check.sh
#
# CI (.github/workflows/ci.yml) runs exactly this script, so a local pass means
# a CI pass. The shared steps come from scripts/lib/gate.sh (managed by
# repo-template); this repository's own steps import the package, byte-compile
# it and run the unit tests, which need no hardware.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=lib/gate.sh
. scripts/lib/gate.sh

gate_toolchain
gate_python

step "import smoke test"
"$VENV/bin/python" -c "import pyscoppy; from pyscoppy import protocol, client, dclient, daemon, webgui, cli, serial_port"

step "byte-compile"
"$VENV/bin/python" -m compileall -q pyscoppy run.py tests
echo "OK"

step "unit tests"
# Extra arguments go to the test runner: ./scripts/check.sh -v
"$VENV/bin/python" -m unittest discover -s tests "$@"
gate_shellcheck
gate_secret_scan
gate_done
