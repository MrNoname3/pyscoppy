# Orientation for AI agents working in this repo

This is a host-side driver for a **Scoppy** USB oscilloscope (a Raspberry Pi Pico,
here on an FHDM FScope-500K analog board). Its whole point is **shared use**: one
daemon owns the single serial connection and relays the live stream + control over
a local Unix socket, so a **human (in a browser GUI) and you (an AI agent) drive
the same scope at once** and stay in sync. Read this first; read
[PROTOCOL.md](PROTOCOL.md) if you touch the wire protocol.

## Working with a human, live

The usual setup: the human runs `python run.py` (daemon + web GUI) and watches the
trace in a browser. You connect to the **same daemon** and share their view.

**First, check the daemon is up** — nothing works without it:

    python3 -m pyscoppy state      # prints the shared settings (or says it's not running)

- If it prints state, you're in the live session — go ahead.
- If it says it's not running, ask the human to start it (`python run.py`), or, if
  you're headless, start it yourself in the **background** so it outlives the tool
  call that launched it: `python3 -m pyscoppy daemon`. **Never start a second
  daemon** — only one host can hold `/dev/ttyACM0`; a second just fights for it.

**Your eyes** (the same data the human sees in the GUI):

    python3 -m pyscoppy state          # current settings: channels, timebase, trigger, ranges…
    python3 -m pyscoppy stream         # live per-channel voltage stats
    python3 -m pyscoppy grab --plot    # grab a chunk of samples + an ASCII plot

**Your hands** — a setting change is applied to the Pico *and broadcast to the
human's GUI*, tagged as changed by `agent`:

    python3 -m pyscoppy set --run stop
    python3 -m pyscoppy set --channels 0,1 --trigger auto --sample-rate 500000

**Etiquette:** every change alters the human's live view, and hardware changes
(input range, signal generator, trigger, sample rate) change what the instrument
is physically doing — so say what you're about to change and why before you do it.
The human's own changes come back to you the same way (tagged `web`), so you can
follow along in `stream`. `info` (device identity) talks to the serial directly,
so it only works while the daemon is **stopped** — don't run it mid-session.

## What works today (validated on live firmware v18)

- The framing parser/encoder, `SYNC`/`SAMPLES` decoders, and the v18 auth token
  (`compute_auth_token`, `build_sync_response_v18`) in
  [`pyscoppy/protocol.py`](pyscoppy/protocol.py) all match the hardware.
- Calibrated two-channel capture, trigger, timebase/sample-rate/range control,
  signal generator, and a browser scope (YT/XY/FFT, cursors, math, the full
  measurement set, CSV export, logic-analyzer mode). See [README.md](README.md).

## Key facts that took real work to find

- **v18 handshake needs an auth token** (4 bytes) and response version 3 — see
  PROTOCOL.md §6b. Token = `MD5(str(nonce+693)+"Err[45]:9397")[:4]`, nonce from
  SYNC offset 14.
- **SAMPLES channel byte** packs channel in the LOW nibble, range in the HIGH
  nibble (`ch_id | range<<4`) — the OPPOSITE order from the SYNC calibration
  table. Don't unify them (see `decode_samples`).

## Practical gotchas (these cost real debugging time)

- **CLOCAL is mandatory.** Without it, `select()` reports the tty as not writable
  and `os.write` writes 0 bytes — silently. See `serial_port.py`.
- **Sending many malformed `SYNC_RESPONSE`s can wedge the firmware** until it is
  physically replugged (it goes silent — 0 bytes). When debugging the handshake,
  send *one* candidate, watch for a type-61 frame for a couple of seconds, and ask
  for a replug between rounds rather than flooding.
- Only **one** USB host can hold `/dev/ttyACM0`; close your handle between runs.
- The device is `/dev/ttyACM0` (`2e8a:000a`).

## Repo conventions

- **stdlib-only** — no third-party packages at all (the `grab --plot` output is a
  built-in ASCII renderer, not matplotlib). Keep it that way so it runs anywhere.
- Code and docs in English. The package type-checks clean under Pyright / Pylance
  "basic" (`pyrightconfig.json`) — keep it green. Update PROTOCOL.md's validation
  status when you confirm or refute something on real hardware.
