# Orientation for AI agents working in this repo

You are looking at a host-side driver for a **Scoppy** oscilloscope running on a
USB-connected Raspberry Pi Pico. Read this first, then [PROTOCOL.md](PROTOCOL.md).

## What works today (all validated on live firmware v18)

- A **daemon** (`scoppyd`) owns the serial port and completes the authenticated
  v18 handshake; the CLI, web GUI and agents share its live stream over a local
  Unix socket. Start it with `./run-daemon.sh`, the GUI with `./run-gui.sh`.
- CLI verbs (`python3 -m pyscoppy …`): `state`, `stream`, `grab`, `set`, `info`
  (run `info` only while the daemon is stopped — it reads the serial directly).
- The framing parser/encoder, `SYNC`/`SAMPLES` decoders, and the v18 auth token
  (`compute_auth_token`, `build_sync_response_v18`) in
  [`pyscoppy/protocol.py`](pyscoppy/protocol.py) all match the hardware.

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

- Core driver is **stdlib-only** (no pyserial); keep it that way so it runs
  anywhere. `matplotlib`/`numpy` may be optional extras for plotting only.
- Code comments and docs in English. Update PROTOCOL.md's validation status when
  you confirm or refute something on real hardware.
