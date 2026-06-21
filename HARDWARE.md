# Hardware & connectivity notes

## The device in this repo

- Board: **FHDM FScope-500K** — a 2-channel scope board with a real analog
  front-end (±6V/1X, 1MΩ/22pF, AC/DC coupling, built-in signal generator),
  carrying a regular Pico. USB id `2e8a:000a`, `/dev/ttyACM0`.
- Firmware: **`scoppy-fscope-500k-pico-v18.uf2`** (in [`firmware/`](firmware/)).
  This board needs its OWN firmware — the generic `scoppy-pico` build breaks
  calibration and CH2 (see the warning below). Reflash by holding BOOTSEL while
  plugging in, then copy the `.uf2` to the `RPI-RP2` mass-storage drive.

> **⚠️ Flash the right firmware.** Putting the generic `scoppy-pico-*.uf2` on an
> FScope board makes it read the front-end output raw, uncalibrated (3.3 V shows
> as ~2.6 V), report `voltage ranges: 0`, and leaves CH2 dead. The official app
> shows the same breakage. The fix is the `scoppy-fscope-500k-*` firmware.

## Analog inputs (oscilloscope mode)

The board/app label the channels **CH1 / CH2**; internally they are firmware ids
**0 / 1**. Each channel has a switchable front-end gain — the firmware uploads the
**voltage-range calibration** (min/max volts per range) in the SYNC message; the
host converts `adc` → volts with `min_v + adc/255 × (max_v − min_v)` for the
active range. Example ranges (per channel): ±5.9 V, ±2.3 V, ±1.0 V, ±0.5 V.

- **Input range ±6 V** (1X) through the front-end — far safer than a bare Pico's
  raw 0–3.3 V ADC pins.
- Samples are **8-bit** (`adc12 >> 4`).
- Specs (per Scoppy): max **500 kS/s** shared across channels, ~150 kHz analog
  bandwidth, memory 2k–20k pts (up to 100k single-shot). Logic analyzer: 25 MS/s.
- The voltage-range select GPIOs (GP2–GP5 in the firmware) are only meaningful on
  boards with a switchable analog front-end; on a bare Pico the range id is 0.

## USB vs Wi-Fi — and why you can't use both at once

The firmware serves **one transport and one client session at a time**:

- It tries **USB first**; only if no USB connection is established within ~10 s
  does a Pico **W** start listening on Wi-Fi. (Wiki: *"if a connection over USB
  hasn't been established within 10 seconds the Pico W will start listening to
  connections over Wi-Fi."*)
- All scope state (run mode, trigger, sample rate, channels) is global to a single
  session in the firmware — there is no second concurrent client.

**Consequence:** you **cannot** have this driver read the Pico over USB while a
phone simultaneously views the *same* Pico over Wi-Fi. They are mutually
exclusive. For "AI reads + human watches" at the same time, use **two Picos**
(~$5 each): one on USB for the driver, one on Wi-Fi for the app.

## Wi-Fi (Pico W / Pico 2 W only)

The plain Pico flashed here has **no Wi-Fi hardware**. Wi-Fi requires a **Pico W**
or **Pico 2 W** and the **`scoppy-picow-*.uf2`** firmware build. When you have
that, two Wi-Fi modes exist (chosen in the app's firmware settings, not at
power-on):

- **Access Point:** the Pico W creates its own `SCOPPY` network; you join it from
  the phone's Wi-Fi settings. LED: 4 blinks + 1–2 s pause = AP mode, waiting.
- **Station / Client (recommended):** the Pico W joins your existing home network
  (enter SSID + password + auth, e.g. WPA2/WPA Mixed). Phone and Pico then find
  each other on the same LAN.

Switching USB↔Wi-Fi is done in the app: badge → *Change connection type*.

## Sources

- Scoppy site & wiki: https://oscilloscope.fhdm.xyz/
- Getting started (Pico W / Wi-Fi): https://oscilloscope.fhdm.xyz/wiki/Getting-started-with-the-Pico-W.html
- Firmware source (GPL-3.0, v8/v10 snapshot): https://github.com/zaheeroz/scoppy-pico-Oscilloscope
