"""Command line interface for pyscoppy.

Everything runs through the shared daemon (scoppyd), so the CLI, the web GUI and
the agent share one live connection:

    python3 -m pyscoppy up            # one-click: daemon + GUI in one process
    python3 -m pyscoppy daemon        # start just the shared daemon (owns the serial port)
    python3 -m pyscoppy gui           # serve just the web GUI -> http://127.0.0.1:8077
    python3 -m pyscoppy state         # current shared settings
    python3 -m pyscoppy stream        # live stats from the daemon
    python3 -m pyscoppy grab --plot    # sniff a chunk of the shared signal
    python3 -m pyscoppy set --run stop # change a setting; everyone sees it

    python3 -m pyscoppy info          # device identity (direct serial; needs the daemon stopped)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import TYPE_CHECKING, Any, Optional, Sequence

from . import protocol as P

if TYPE_CHECKING:
    from .dclient import DaemonClient


def cmd_info(args: argparse.Namespace) -> int:
    # direct serial read — only when the daemon is NOT running (it owns the port)
    from .client import ScoppyClient
    from .serial_port import find_port
    with ScoppyClient(find_port(args.port)) as c:
        info = c.read_device_info(timeout=4.0)
        if not info:
            print("No SYNC seen. Is the Pico connected? Is the daemon holding the port?")
            return 1
        print("Scoppy device:")
        for k in ("firmware_type", "firmware_version", "build_number",
                  "auto_voltage_range", "num_voltage_ranges"):
            print(f"  {k:18}: {info[k]}")
        print(f"  chip id           : 0x{info['chip_id']:08x}")
        print(f"  unique id         : {info['unique_id']}")
        if info.get("voltage_ranges"):
            print("  voltage ranges (V):")
            for k, (lo, hi) in sorted(info["voltage_ranges"].items()):
                print(f"    ch{k}: {lo:+.3f} .. {hi:+.3f}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run
    run(port=args.port)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .webgui import run
    run(host=args.host, port=args.gui_port)
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    """One-click launcher: daemon + GUI in a single process.

    --no-gui   runs only the daemon; --gui-only serves only the GUI (assumes a
    daemon is already up). Default starts both and ties their lifetimes together,
    so one Ctrl-C / stop shuts everything down.
    """
    import signal
    import threading
    from . import webgui
    from .daemon import Daemon
    from .dclient import is_daemon_running

    if args.gui_only:
        webgui.run(host=args.host, port=args.gui_port)
        return 0

    if is_daemon_running():
        # a daemon is already up — don't start a second one (they'd fight over the
        # serial port). Just attach the GUI to it, so F5 / `up` still "just works".
        if args.no_gui:
            print("scoppyd is already running — nothing to do.")
            return 0
        print("scoppyd is already running — serving the GUI against it.", flush=True)
        webgui.run(host=args.host, port=args.gui_port)
        return 0

    daemon = Daemon(port=args.port)

    def _serve_daemon() -> None:
        daemon.connect_device()
        daemon.serve()

    dthread = threading.Thread(target=_serve_daemon, name="scoppyd", daemon=True)
    dthread.start()

    if args.no_gui:
        try:
            while dthread.is_alive():
                dthread.join(0.5)
        except KeyboardInterrupt:
            pass
        daemon.running = False
        dthread.join(timeout=2.0)
        return 0

    # wait for the daemon's socket to come up, then serve the GUI in this thread
    for _ in range(100):
        if is_daemon_running():
            break
        time.sleep(0.05)
    srv = webgui.make_server(args.host, args.gui_port)
    print(f"scoppy: daemon + web GUI up  ->  http://{args.host}:{args.gui_port}"
          f"   (Ctrl-C / stop to quit)", flush=True)

    def _on_sigterm(*_: object) -> None:   # VSCode's stop button / `kill` -> clean shutdown
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nscoppy: shutting down…", flush=True)
        srv.server_close()
        daemon.running = False
        dthread.join(timeout=2.0)
    return 0


# -- daemon client commands ------------------------------------------------

def _dclient() -> Optional[DaemonClient]:
    from .dclient import DaemonClient, is_daemon_running
    if not is_daemon_running():
        print("scoppyd not running. Start it with:  python3 -m pyscoppy daemon")
        return None
    return DaemonClient(role="agent")


def _stats(samples: Sequence[int]) -> Optional[tuple[int, int, float]]:
    if not samples:
        return None
    return min(samples), max(samples), sum(samples) / len(samples)


def _ascii_plot(samples: Sequence[int], rate: int, channel: int,
                width: int = 80, height: int = 16) -> None:
    step = max(1, len(samples) // width)
    cols = [samples[i] for i in range(0, len(samples), step)][:width]
    grid = [[" "] * len(cols) for _ in range(height)]
    for x, s in enumerate(cols):
        grid[height - 1 - int(s / 255 * (height - 1))][x] = "*"
    print(f"CH{channel + 1}  ~{rate} S/s  {len(samples)} samples  ({len(cols)} cols)")
    print("hi +" + "-" * len(cols) + "+")
    for row in grid:
        print("   |" + "".join(row) + "|")
    print("lo +" + "-" * len(cols) + "+")


def cmd_state(args: argparse.Namespace) -> int:
    c = _dclient()
    if not c:
        return 1
    st = c.get_state()
    c.close()
    print(json.dumps(st, indent=2))
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    c = _dclient()
    if not c:
        return 1
    c.subscribe()
    print(f"Streaming from daemon for {args.seconds}s...")
    end = time.monotonic() + args.seconds
    for msg in c.messages(timeout=0.5):
        if msg.get("type") == "frame":
            data = msg["channels"].get(str(args.channel))
            calmap: dict[str, Any] = msg.get("cal") or {}
            cal = calmap.get(str(args.channel), [0.0, P.ADC_VREF])
            st = _stats(data) if data else None
            if st:
                def v(s: float) -> float:
                    return cal[0] + s / 255 * (cal[1] - cal[0])
                print(f"  CH{args.channel + 1} rate~{msg['rate']} S/s  "
                      f"min={v(st[0]):.3f}V max={v(st[1]):.3f}V avg={v(st[2]):.3f}V")
        if time.monotonic() >= end:
            break
    c.close()
    return 0


def cmd_grab(args: argparse.Namespace) -> int:
    c = _dclient()
    if not c:
        return 1
    m = c.grab(channel=args.channel, n=args.count)
    c.close()
    if not m or not m["data"]:
        print("No data (is the daemon synced and streaming?).")
        return 1
    buf = m["data"]
    st = _stats(buf)
    if st:
        print(f"Grabbed {len(buf)} samples (~{m['rate']} S/s) CH{args.channel + 1}: "
              f"adc min={st[0]} max={st[1]} avg={st[2]:.0f}")
    if args.plot:
        _ascii_plot(buf, m["rate"], args.channel)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    c = _dclient()
    if not c:
        return 1
    params: dict[str, Any] = {}
    if args.channels is not None:
        params["channels"] = [int(x) for x in args.channels.split(",")]
    if args.timebase is not None:
        params["timebase_centi_us"] = args.timebase
    if args.run is not None:
        params["run_mode"] = {"run": P.RUN, "stop": P.STOP, "single": P.SINGLE}[args.run]
    if args.trigger is not None:
        params["trig_mode"] = {"none": P.TRIG_NONE, "auto": P.TRIG_AUTO, "normal": P.TRIG_NORMAL}[args.trigger]
    if args.sample_rate is not None:
        params["sample_rate"] = args.sample_rate
    c.send({"cmd": "set", "params": params})
    latest: Any = None
    for msg in c.messages(timeout=2.0):
        if msg.get("type") == "state":
            latest = msg["state"]
            if msg.get("by") == "agent":
                break
    c.close()
    print("New state:", json.dumps(latest) if latest else "(no reply)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pyscoppy")
    ap.add_argument("--port", default="/dev/ttyACM0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="device identity (direct serial; needs the daemon stopped)")
    sub.add_parser("daemon", help="run the shared scoppyd daemon (owns the serial port)")
    sub.add_parser("state", help="print daemon state")

    g = sub.add_parser("gui", help="serve the web GUI (browser oscilloscope)")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--gui-port", type=int, default=8077)

    up = sub.add_parser("up", help="one-click: run the daemon AND serve the GUI together")
    up.add_argument("--port", default="/dev/ttyACM0", help="serial device")
    up.add_argument("--host", default="127.0.0.1")
    up.add_argument("--gui-port", type=int, default=8077)
    mx = up.add_mutually_exclusive_group()
    mx.add_argument("--no-gui", action="store_true", help="run only the daemon")
    mx.add_argument("--gui-only", action="store_true",
                    help="serve only the GUI (a daemon must already be running)")

    ds = sub.add_parser("stream", help="live stats from the daemon")
    ds.add_argument("--channel", type=int, default=0)
    ds.add_argument("--seconds", type=float, default=5.0)

    dg = sub.add_parser("grab", help="grab a chunk from the daemon's buffer")
    dg.add_argument("--channel", type=int, default=0)
    dg.add_argument("--count", type=int, default=2000)
    dg.add_argument("--plot", action="store_true")

    dse = sub.add_parser("set", help="change settings via the daemon")
    dse.add_argument("--channels", help="comma list, e.g. 0,1")
    dse.add_argument("--timebase", type=int, help="timebase in 1/100 us")
    dse.add_argument("--run", choices=["run", "stop", "single"])
    dse.add_argument("--trigger", choices=["none", "auto", "normal"])
    dse.add_argument("--sample-rate", type=int, help="fixed sample rate in S/s (0 = Auto)")

    args = ap.parse_args(argv)
    return {
        "info": cmd_info,
        "daemon": cmd_daemon,
        "gui": cmd_gui,
        "up": cmd_up,
        "state": cmd_state,
        "stream": cmd_stream,
        "grab": cmd_grab,
        "set": cmd_set,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
