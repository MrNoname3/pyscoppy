#!/usr/bin/env python3
"""One-click launcher for the Scoppy oscilloscope.

Run this file (VSCode's Run ▷ button, or `python run.py`) to start the shared
daemon AND serve the web GUI in a single process, then open the printed URL.
Stopping it (Ctrl-C, or VSCode's stop button) shuts both down together.

Switches (also work via `python run.py --help`):

    python run.py                 # daemon + GUI  (default)
    python run.py --no-gui        # only the daemon
    python run.py --gui-only      # only the GUI  (a daemon must already run)
    python run.py --port /dev/ttyACM1 --gui-port 8080
"""
import os
import sys

# make `import pyscoppy` work no matter what the current directory is
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyscoppy.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["up"] + sys.argv[1:]))
