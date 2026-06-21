# Scoppy USB serial protocol (reverse-engineered)

This document describes the wire protocol spoken between the **Scoppy firmware**
running on a Raspberry Pi Pico and the host (normally the Scoppy Android app),
over the Pico's **USB CDC ACM** serial interface (`/dev/ttyACM*`).

It was reconstructed from the GPL-3.0 firmware source (`fhdm-dev/scoppy-pico`,
a v8/v10 snapshot) plus live protocol analysis, and **validated against a real
v18 Pico**.

> **Validation status**
> - ✅ Framing (both directions): confirmed live.
> - ✅ `SYNC` (device identity) decode: confirmed live (firmware_version byte reads 18).
> - ✅ `SAMPLES` decode: derived from source (validated once streaming works).
> - ✅ **v18 handshake WORKS (validated live).** v18 added a **4-byte auth token**
>   to `SYNC_RESPONSE` (where v10 had "reserved" bytes) and bumped the response
>   **version to 3**. With the token computed and sent, the Pico syncs and streams
>   `SAMPLES`; the driver decoded ~21k calibrated samples at 5 kS/s on channel 0.
>   See §6b.

---

## 1. Transport

- Pure **USB CDC ACM**, two interfaces: CDC control (class 0x02/0x02) + CDC data
  (class 0x0a). Vendor:product = `2e8a:000a` (Raspberry Pi / Pico).
- Appears as `/dev/ttyACM0` on Linux. Baud rate is irrelevant (USB CDC).
- **You must set `CLOCAL`** on the tty and assert **DTR**/**RTS**, otherwise
  host→device writes are silently gated and never reach the firmware. (This bit
  us during development — see [`pyscoppy/serial_port.py`](pyscoppy/serial_port.py).)
- The Pico **broadcasts `SYNC` messages continuously** while it waits for a host,
  so you can read device identity without any handshake.

## 2. Frame format

Both directions share a 6-byte header. **Integers are big-endian.**

| offset | field        | notes                                         |
|--------|--------------|-----------------------------------------------|
| 0      | start byte   | always `255` (0xff)                           |
| 1–2    | `msg_size`   | uint16, total frame length (see below)        |
| 3      | `msg_type`   |                                               |
| 4      | `msg_type+5` | redundancy/sanity check                       |
| 5      | `msg_version`| ≥ 1                                           |
| 6…     | payload      |                                               |
| last   | end byte     | `86` (0x56) — **host → Pico frames only**     |

**Asymmetry to remember:**
- **Pico → host** frames have **no end byte**. `msg_size = 6 + payload_len`.
- **Host → Pico** frames **must** end with byte `86`. `msg_size = 6 + payload_len + 1`,
  and that end byte is *included* in `msg_size`. `msg_size` must be ≥ 5.

The check byte (`msg_type + 5`, mod 256) lets a parser reject a false start-byte
match. Parsers resync by scanning for the next `255`.

## 3. Message types

| type | dir        | name                  |
|------|------------|-----------------------|
| 60   | Pico→host  | SYNC                  |
| 61   | Pico→host  | SAMPLES               |
| 80   | host→Pico  | SYNC_RESPONSE         |
| 81   | host→Pico  | HORZ_SCALE_CHANGED    |
| 82   | host→Pico  | CHANNELS_CHANGED      |
| 83   | host→Pico  | TRIGGER_CHANGED       |
| 84   | host→Pico  | SIG_GEN               |
| 85   | host→Pico  | SELECTED_SAMPLE_RATE  |
| 87   | host→Pico  | PRE_TRIGGER_SAMPLES   |
| 88   | host→Pico  | VOLTAGE_RANGE_CHANGED |
| 89   | host→Pico  | SYNC_REQUIRED         |

## 4. Handshake / state machine

The firmware is a two-state machine (`scoppy-lib/scoppy.c` in the firmware source):

```
UNSYNCED:  loop { send SYNC; sleep; read incoming for ~1s (with backoff);
                  if got valid SYNC_RESPONSE -> SYNCED }
SYNCED:    run main sampling loop -> stream SAMPLES; on loss -> UNSYNCED
```

So to drive it: read a `SYNC`, send a valid `SYNC_RESPONSE`, then read `SAMPLES`.
There is exactly **one** client/session — see [README](README.md) on why USB and
Wi-Fi can't both be active.

## 5. SYNC payload (type 60, Pico → host) — device identity

Confirmed against v18 (extra trailing bytes appended by v18 are ignored).

| offset | size | field            |
|--------|------|------------------|
| 0      | 4    | chip id (uint32) |
| 4      | 8    | unique id        |
| 12     | 1    | firmware type    |
| 13     | 1    | firmware version | ← reads **18** on the test device |
| 14     | 4    | build number (int32) |
| 18     | 1    | flags (bit0 = auto voltage range) |
| 19     | 1    | num voltage ranges |
| 20…    | 9×n  | per range: `chan<<4|range` (1), min_uV (int32), max_uV (int32) |
| …      | +15  | v18: extra trailing bytes (purpose TBD) |

## 6. SYNC_RESPONSE payload (type 80, host → Pico) — v10 layout

> ⚠️ This is the **v10** layout, kept for reference. v18 rejects it; use the
> auth-token variant in §6b.

| offset | size | field |
|--------|------|-------|
| 0      | 1    | flags: bits0–1 = run_mode (0=run,1=stop,2=single); bits2–3 = app_mode (0=scope, >0=logic) |
| 1      | 4    | reserved (ignored) |
| 5      | 1    | num_channels (1–8) |
| 6      | n    | per channel config byte: bit0 = enabled |
| 6+n    | 2    | skipped |
| 8+n    | 4    | timebase (uint32, in 1/100 µs; `timebase_ps = value × 10000`) |
| 12+n   | 1    | trigger mode (0=none,1=auto,2=normal) |
| 13+n   | 1    | trigger channel |
| 14+n   | 1    | trigger type (0=rising,1=falling) |
| 15+n   | 2    | trigger level (int16, 0–255) |

A 1-second timebase (`value = 100_000_000`) selects low-rate **continuous**
streaming, the easiest mode to consume.

## 6b. SYNC_RESPONSE on v18 — auth token

v18 firmware **validates** the bytes v10 ignored. The host must build:

```
version byte (offset 5 in the frame) = 3          # v10 used 1
payload[0]   = (app_mode << 2) | run_mode          # app_mode 0=scope,1=logic
payload[1..4]= auth token                          # <-- NEW vs v10
payload[5]   = num_channels
payload[6..] = per-channel byte (bit0 = enabled)
   ... then (offset K after channels):
payload[K]   = sample-rate/app variant byte (usually 0)
payload[K+1] = 0
payload[K+2..K+5] = timebase (uint32 BE, 1/100 µs)
payload[K+6..]    = trigger: mode, channel, type, level(int16 BE)
payload[..], [..] = two trailing bytes (default 0,0)
```

**Auth token** (the crux):

```
token = first 4 bytes, big-endian, of  MD5( str(challenge + 693) + "Err[45]:9397" )
```

`challenge` is a **per-session nonce** the firmware generates each boot and sends
in the SYNC message — the 4-byte big-endian field at **SYNC payload offset 14**
(what §5 calls "build number"; it changes on every replug, confirming it's a
nonce). One code path uses `challenge - 237` instead; the driver tries both. The
salt `"Err[45]:9397"` and offset `693` are protocol constants.

Implemented in [`pyscoppy/protocol.py`](pyscoppy/protocol.py)
(`compute_auth_token`, `build_sync_response_v18`). The channel/trigger
sub-encodings match v10.

## 7. SAMPLES payload (type 61, Pico → host)

Header then raw interleaved 8-bit samples:

| offset | size | field |
|--------|------|-------|
| 0      | 1    | flags: bit0 new record, bit1 last-in-frame, bit2 continuous, bit3 single-shot, bit4 logic |
| 1      | 1    | num_data_channels |
| 2      | n    | per channel: `chan_id | (voltage_range << 4)` |
| 2+n    | 4    | real sample rate per channel (uint32, Hz) |
| 6+n    | 4    | trigger index (int32, −1 if none) |
| 10+n   | …    | samples: 1 byte/channel/point, interleaved ch0,ch1,ch0,ch1,… |

**Sample encoding:** the RP2040 12-bit ADC value is shifted to 8 bits
(`adc >> 4`). On an unmodified Pico the input range is **0–3.3 V**, so
`volts = sample / 255 × 3.3`. Channel→pin: **CH0 = GP26 (ADC0)**, **CH1 = GP27 (ADC1)**.

## 8. Other host → Pico messages (payloads)

- **CHANNELS_CHANGED (82):** num_channels(1), then config bytes (bit0 enabled).
- **TRIGGER_CHANGED (83):** mode(1), channel(1), type(1), level(int16).
- **HORZ_SCALE_CHANGED (81):** timebase (uint32, 1/100 µs).
- **SELECTED_SAMPLE_RATE (85):** rate Hz (uint32). 0 = auto. `< 2000` forces
  continuous mode.
- **PRE_TRIGGER_SAMPLES (87):** percent 0–100 (1 byte).
- **SIG_GEN (84):** function(1), gpio(1), freq Hz(uint32), duty(uint16).
- **SYNC_REQUIRED (89):** no payload; asks the firmware to resync.

## Open questions

1. **The +15 trailing SYNC bytes** on v18: `00×11 02 00 10 00`. Likely new
   capability/version fields; the SYNC_RESPONSE probably gained matching fields.
2. Newer firmware source (v15–v18) is not public; only the v8/v10 snapshot is.

> Note: don't brute-force the handshake against a live device — it wedges easily
> (goes silent, needs a physical replug). Send one candidate and watch for a reply.
