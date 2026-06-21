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
- If it's not running, bring up the **whole session yourself** so the human only
  has to click a link. Start the combined daemon + GUI **detached** (it must
  outlive the tool call that launches it — in Claude Code run it as a background
  process; otherwise `nohup … &` / `setsid`):

      python3 -m pyscoppy up            # daemon + GUI together (same as run.py)

  Then confirm it came up (`python3 -m pyscoppy state` returns settings) and give
  the human the URL: **http://127.0.0.1:8077**. If they're on another machine,
  start it with `python3 -m pyscoppy up --host 0.0.0.0` and hand them
  `http://<this-host-LAN-IP>:8077`. **Never start a second daemon** — only one host
  can hold `/dev/ttyACM0`; the launcher refuses if one is already up.

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

## What you can set — and what only the human can

**Shared settings** live on the device and are broadcast to the human's GUI, so
**you can push them** and the human sees them change. The CLI `set` covers the
common ones:

    python3 -m pyscoppy set --channels 0,1 --trigger auto --sample-rate 500000 --run run

For the knobs the CLI doesn't expose, send any of these keys straight to the
daemon (one call), then read `state` to confirm:

    python3 -c "from pyscoppy.dclient import DaemonClient as D; c=D(role='agent'); \
      c.set(trig_type=1, trig_level=200, pre_trigger=25, vrange={'0':2}); c.close()"

| key | meaning | values |
|-----|---------|--------|
| `channels` | enabled channels | list, e.g. `[0,1]` |
| `timebase_centi_us` | time/div base | int, units of 1/100 µs |
| `sample_rate` | fixed rate (0 = Auto) | Hz; `< 2000` forces continuous mode |
| `max_sr` | device rate ceiling | code 0=500k, 2=1.3M, 4=2M, 5=2.5M |
| `run_mode` | run / stop / single | 0 / 1 / 2 |
| `trig_mode` | none / auto / normal | 0 / 1 / 2 |
| `trig_type` | edge | 0 rising / 1 falling |
| `trig_level` | trigger level | **raw 0–255**, not volts |
| `trig_channel` | trigger source | channel id |
| `pre_trigger` | pre-trigger | percent 0–100 |
| `vrange` | front-end input range per ch | `{"0": id}` — lower id = wider volts |
| `auto_vrange` | auto-pick range per ch | `{"0": true}` |
| `logic_mode` | scope ↔ logic analyzer | bool |

The signal generator is its own command (func: 0 off, 1 square, 2 sine):
`c.send({"cmd":"siggen","func":1,"gpio":22,"freq":1000,"duty":50})` (GP22 on the
FScope; gpio 255 = firmware default).

**GUI-local settings are browser-only — you CANNOT push them** to a running
browser (there's no daemon command for them); you can only ask the human to click,
or change their behaviour by editing `pyscoppy/web/` (that's developing the GUI,
not driving a live one). These are display-side (kept in the browser, not the
daemon): Volts/Div
display zoom, vertical/horizontal **position**, **probe** factor, **cursors**, the
**view** (YT / XY / FFT / combined), **FFT** window/scale/span, the **math** op,
which **measurements** show, trace width, roll mode. So to make the human's screen
match yours: set the shared knobs above, then tell them which display buttons to
press.

## What data you get, and how to read it

`grab` returns raw 8-bit ADC counts for one channel (one device record):

    {"channel": 0, "rate": 500000, "data": [0..255, …]}

`stream` / the live frame carries every enabled channel plus calibration and
measurements:

    {"type":"frame", "rate":<Hz>, "channels":{"0":[0..255,…]},
     "cal":{"0":[min_v,max_v]}, "meas":{"0":{"min":…,"max":…,"freq":…, …}}}

Convert a raw sample to volts with that channel's active range:

    volts = cal_min + sample / 255 * (cal_max - cal_min)

`grab` omits `cal`, so take it from `state["voltage_ranges"]` for the channel's
current `vrange`, or just read one `stream` frame (it already includes `cal`). A
bare Pico has no front-end → range is 0–3.3 V (`cal = [0, 3.3]`).

**Scope vs logic analyzer.** Scope mode (default) gives calibrated analog volts as
above. Switch to the **logic analyzer** with `logic_mode=True`: the frame then
carries `"logic": [byte, …]` instead of `channels`, each byte = 8 digital inputs —
bit *b* is **D*b* = GP(6+*b*)**, so D0–D7 = GP6–GP13. No volts, just highs/lows.
See [HARDWARE.md](HARDWARE.md) for the analog ranges and full pin map.

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
