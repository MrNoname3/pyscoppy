#!/bin/sh
# Start the shared scoppyd daemon (owns the serial port; stdlib only, no venv).
cd "$(dirname "$0")"
exec python3 -m pyscoppy daemon "$@"
