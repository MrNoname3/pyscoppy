"""pyscoppy - a dependency-free Python driver for the Scoppy Pico oscilloscope.

Talks the Scoppy USB serial protocol directly, so an automated agent (or a
human) can drive a USB-connected Pico running Scoppy firmware without the
Android app. See README.md and PROTOCOL.md.
"""

from .client import ScoppyClient
from . import protocol

__all__ = ["ScoppyClient", "protocol"]
__version__ = "0.1.0"
