#!/bin/sh
# Local dev checks — the same gates CI enforces. The CI workflow
# (.github/workflows/ci.yml) calls this script for the compile + test steps, so
# there is one source of truth for "is the code OK". Run it before pushing:
#
#     ./run-checks.sh            # compile + unit tests + type check
#     ./run-checks.sh -v         # extra args go to the test runner (verbose)
#
# Everything here is stdlib-only except pyright, which is skipped (with a hint)
# when it isn't installed — so the script always runs the tests.
set -e
cd "$(dirname "$0")"

echo "==> byte-compile"
python3 -m compileall -q pyscoppy run.py tests

echo "==> unit tests"
python3 -m unittest discover -s tests "$@"

echo "==> type check (pyright, strict)"
if command -v pyright >/dev/null 2>&1; then
    pyright
else
    echo "    pyright not on PATH — skipping (pip install pyright to enable)"
fi

echo "==> done"
