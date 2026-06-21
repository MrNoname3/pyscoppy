#!/bin/sh
# Serve the web GUI (stdlib only). Needs the daemon running. Open the printed URL.
cd "$(dirname "$0")"
exec python3 -m pyscoppy gui "$@"
