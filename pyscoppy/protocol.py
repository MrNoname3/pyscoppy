"""Scoppy wire protocol (reverse-engineered).

Source of truth: the GPL-3.0 Scoppy firmware (fhdm-dev/scoppy-pico), plus live
protocol analysis against a real v18 Pico. The framing was confirmed against
the device, and the SYNC payload decodes correctly there too (firmware_version
byte reads 18). See PROTOCOL.md for the full spec and the v18 caveats.

All multi-byte integers are big-endian ("network bytes").
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

START_BYTE = 255          # 0xff  - first byte of every frame (both directions)
END_BYTE = 86             # 0x56  - terminator, ONLY on host->Pico frames

# Pico -> host
MSG_SYNC = 60
MSG_SAMPLES = 61
DEVICE_MSG_TYPES = (MSG_SYNC, MSG_SAMPLES)   # the only types the Pico sends to us

# host -> Pico
MSG_SYNC_RESPONSE = 80
MSG_HORZ_SCALE_CHANGED = 81
MSG_CHANNELS_CHANGED = 82
MSG_TRIGGER_CHANGED = 83
MSG_SIG_GEN = 84
MSG_SELECTED_SAMPLE_RATE = 85
MSG_PRE_TRIGGER_SAMPLES = 87
MSG_VOLTAGE_RANGE_CHANGED = 88
MSG_SYNC_REQUIRED = 89

# run modes
RUN = 0
STOP = 1
SINGLE = 2

# trigger modes / types
TRIG_NONE = 0
TRIG_AUTO = 1
TRIG_NORMAL = 2
EDGE_RISING = 0
EDGE_FALLING = 1

# Device "max sample rate" setting (RP2040 Max. Sample Rate in the app). Sent as a
# single code byte in the SYNC_RESPONSE (right after the per-channel bytes); the
# firmware caps its sampling engine at the matching ceiling. Code 0 (500 kS/s) is
# the safe default; raising it lets the v18 firmware overclock the ADC well past
# 500 kS/s.
MAX_SR_CODES = {0: 500_000, 2: 1_300_000, 4: 2_000_000, 5: 2_500_000}
MAX_SR_LABELS = {0: "500 kS/s", 2: "1.3 MS/s", 4: "2 MS/s", 5: "2.5 MS/s"}
MAX_SR_DEFAULT = 0

# Hardware (bare Pico): channel -> ADC GPIO
CH_GPIO = {0: 26, 1: 27}
ADC_VREF = 3.3            # full-scale voltage for an unmodified Pico
ADC_FULL_SCALE = 255      # samples are 8-bit (adc12 >> 4)


# --------------------------------------------------------------------------
# Encoding host -> Pico frames
# --------------------------------------------------------------------------

def encode_message(msg_type: int, payload: bytes | bytearray, version: int = 1) -> bytes:
    """Build a complete host->Pico frame (with trailing END_BYTE).

    Wire layout: START, size_hi, size_lo, type, type+5, version, payload..., END
    `size` is the total frame length from START through END inclusive.
    """
    body = bytes(payload) + bytes([END_BYTE])
    size = 6 + len(body)                    # 6 header bytes + payload + end byte
    header = bytes([START_BYTE, (size >> 8) & 0xFF, size & 0xFF,
                    msg_type, (msg_type + 5) & 0xFF, version])
    return header + body


def build_sync_response(channels: Sequence[int] = (0,), run_mode: int = RUN,
                        logic_mode: bool = False,
                        timebase_centi_us: int = 100_000_000,
                        trig_mode: int = TRIG_NONE, trig_channel: int = 0,
                        trig_type: int = EDGE_RISING, trig_level: int = 128) -> bytes:
    """Payload for MSG_SYNC_RESPONSE.

    `channels` is the set of enabled channel ids (e.g. (0,) or (0, 1)).
    `timebase_centi_us` is the on-screen time period in 1/100 microseconds;
    the firmware derives the sample rate from it (timebase_ps = value * 10000).
    The default (1e8 -> 1 s screen) selects low-rate *continuous* streaming,
    which is the simplest mode to consume.
    """
    app_mode = 1 if logic_mode else 0
    flags = (run_mode & 0x3) | ((app_mode & 0x3) << 2)
    num_channels = max(channels) + 1 if channels else 1
    chan_bytes = bytes(0x01 if ch in channels else 0x00 for ch in range(num_channels))
    p = bytearray()
    p.append(flags)
    p += b"\x00\x00\x00\x00"               # 4 reserved bytes (skipped by firmware)
    p.append(num_channels)
    p += chan_bytes
    p += b"\x00\x00"                        # 2 skipped bytes
    p += struct.pack(">I", timebase_centi_us)
    p.append(trig_mode)
    p.append(trig_channel)
    p.append(trig_type)
    p += struct.pack(">h", trig_level)
    return bytes(p)


# --------------------------------------------------------------------------
# v18 handshake authentication
# --------------------------------------------------------------------------
#
# v18 firmware rejects the old v10 SYNC_RESPONSE because the 4 bytes that v10
# treated as "reserved" (after the flags byte) now carry an auth token that the
# firmware validates. The host must build:
#
#   token_int = first 4 bytes (big-endian) of
#               MD5( str(challenge + 693) + "Err[45]:9397" )
#   SYNC_RESPONSE version byte = 3   (v10 used 1)
#
# `challenge` is read from the incoming SYNC message: a per-session nonce the
# firmware regenerates each boot (the 4-byte field at SYNC payload offset 14 —
# what older notes called "build_number"; it changes on every replug).
import hashlib

AUTH_SALT = "Err[45]:9397"
AUTH_OFFSET = 693
SYNC_RESPONSE_VERSION_V18 = 3
SYNC_NONCE_OFFSET = 14   # 4-byte big-endian nonce inside the SYNC payload


def sync_nonce(sync_payload: bytes) -> int:
    """Extract the per-session challenge nonce from a SYNC (type 60) payload."""
    return int(struct.unpack_from(">I", sync_payload, SYNC_NONCE_OFFSET)[0])


def compute_auth_token(challenge: int) -> bytes:
    """The 4-byte (big-endian int) auth token expected by v18 firmware."""
    s = "%d%s" % ((challenge + AUTH_OFFSET) & 0xFFFFFFFF, AUTH_SALT)
    md5 = hashlib.md5(s.encode()).digest()
    return md5[:4]


def build_sync_response_v18(challenge: int, channels: Sequence[int] = (0,),
                            run_mode: int = RUN, logic_mode: bool = False,
                            timebase_centi_us: int = 100_000_000,
                            trig_mode: int = TRIG_NONE, trig_channel: int = 0,
                            trig_type: int = EDGE_RISING, trig_level: int = 128,
                            max_sr_code: int = MAX_SR_DEFAULT,
                            app_variant: int | None = None,
                            tail: Sequence[int] = (0, 0),
                            num_total_channels: int | None = None) -> bytes:
    """v18 SYNC_RESPONSE payload.

    Layout (payload, i.e. after the 6-byte frame header, before the end byte):
      [0]      flags = (app_mode << 2) | run_mode   (app_mode: 0 scope, 1 logic)
      [1..4]   auth token (4 bytes) = compute_auth_token(challenge)
      [5]      num_channels
      [6..]    per-channel byte (bit0 = enabled)            -> ends at K
      [K]      max-sample-rate code (0=500k, 2=1.3M, 4=2M, 5=2.5M)
      [K+1]    0 (skipped)
      [K+2..5] timebase (uint32 BE, 1/100 µs)
      [K+6..]  trigger: mode(1) channel(1) type(1) level(int16 BE)
      [..]     two trailing bytes (default 0,0)

    NOTE: the channel/trigger sub-encodings are taken to match v10; verify on
    hardware. The auth token + version 3 are the essential v18 changes.
    """
    app_mode = 1 if logic_mode else 0
    flags = (run_mode & 0x3) | ((app_mode & 0x3) << 2)
    # describe enough channels to explicitly turn OFF disabled ones: the firmware only
    # updates the channels it's told about, so sending just the enabled count leaves a
    # previously-enabled channel on (e.g. CH2 stays on after you disable it, pinning the
    # ADC to dual-channel 250 kS/s timing). Always cover all addressable channels.
    min_n = max(channels) + 1 if channels else 1
    num_channels = max(min_n, num_total_channels or min_n)
    p = bytearray()
    p.append(flags)
    p += compute_auth_token(challenge)
    p.append(num_channels)
    p += bytes(0x01 if ch in channels else 0x00 for ch in range(num_channels))
    code = app_variant if app_variant is not None else max_sr_code   # app_variant: legacy alias
    p.append(code & 0xFF)
    p.append(0)
    p += struct.pack(">I", timebase_centi_us)
    p.append(trig_mode)
    p.append(trig_channel)
    p.append(trig_type)
    p += struct.pack(">h", trig_level)
    p += bytes(tail)
    return bytes(p)


def build_trigger_changed(trig_mode: int, trig_channel: int, trig_type: int,
                          trig_level: int) -> bytes:
    """MSG_TRIGGER_CHANGED payload (type 83): mode(1), channel(1), type(1),
    level(int16 BE). Sent live like the app — the firmware updates its trigger
    config and marks itself dirty, so NO re-handshake (which would freeze the
    stream for seconds). Same field order as the SYNC_RESPONSE trigger block."""
    return bytes([trig_mode & 0xFF, trig_channel & 0xFF, trig_type & 0xFF]) + \
        struct.pack(">h", max(0, min(255, int(trig_level))))


def build_horz_scale_changed(timebase_centi_us: int) -> bytes:
    """MSG_HORZ_SCALE_CHANGED payload: timebase in 1/100 µs (uint32 BE).

    Sent live (type 81): the firmware updates timebasePs and marks itself dirty,
    so it recomputes the sample rate on its next loop — no re-handshake needed.
    """
    return struct.pack(">I", timebase_centi_us)


def build_selected_sample_rate(rate_hz: int) -> bytes:
    """MSG_SELECTED_SAMPLE_RATE payload. 0 means auto.

    rate < 2000 forces continuous mode (easy continuous streaming).
    """
    return struct.pack(">I", rate_hz)


def build_pre_trigger_samples(percent: int) -> bytes:
    return bytes([max(0, min(100, percent))])


# Built-in PWM signal generator (firmware drives a GPIO; default pin GP22)
PWM_NONE = 0
PWM_SQUARE = 1
PWM_SINE = 2
SIG_GEN_DEFAULT_GPIO = 255   # 255 = firmware default pin (GP22)


def build_sig_gen(func: int = PWM_SQUARE, gpio: int = SIG_GEN_DEFAULT_GPIO,
                  freq: int = 50, duty: int = 50) -> bytes:
    """MSG_SIG_GEN payload: func(1), gpio(1), freq(uint32 BE), duty(uint16 BE).

    gpio=255 selects the firmware default (GP22). duty is a percentage (square
    wave only). func: PWM_NONE / PWM_SQUARE / PWM_SINE.
    """
    return bytes([func & 0xFF, gpio & 0xFF]) + struct.pack(">I", freq) + struct.pack(">H", duty)


# --------------------------------------------------------------------------
# Decoding Pico -> host frames
# --------------------------------------------------------------------------

class Frame:
    __slots__ = ("msg_type", "version", "payload")

    def __init__(self, msg_type: int, version: int, payload: bytes) -> None:
        self.msg_type = msg_type
        self.version = version
        self.payload = payload

    def __repr__(self) -> str:
        return f"Frame(type={self.msg_type}, ver={self.version}, len={len(self.payload)})"


class FrameReader:
    """Incremental parser for Pico->host frames.

    Pico->host frames have NO end byte. The 2-byte size field is meant to be
    `6 + payload_len`, but v18 firmware over-states it (it always reports a full
    record, ~2065, while actually sending variable-length frames). Trusting it makes
    the reader run past the end and pull the *next* frame's header in as sample bytes
    — which shows up as 0/255 spikes on the trace. So instead of trusting the size we
    delimit each frame at the **next valid header**, and only fall back to the declared
    size as a runaway guard. Feed raw bytes with feed(); pull frames with next().
    """

    _MAX_FRAME = 16384

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes | bytearray) -> None:
        self.buf += data

    def __iter__(self) -> FrameReader:
        return self

    def __next__(self) -> Frame:
        f = self.next()
        if f is None:
            raise StopIteration
        return f

    @staticmethod
    def _header_at(b: bytearray, i: int) -> bool:
        """A valid Pico->host frame header starts at b[i]?"""
        if i + 6 > len(b) or b[i] != START_BYTE:
            return False
        t = b[i + 3]
        if t not in DEVICE_MSG_TYPES or b[i + 4] != ((t + 5) & 0xFF):
            return False
        size = (b[i + 1] << 8) | b[i + 2]
        return 6 <= size <= 0xFFFF

    def _next_header(self, start: int) -> int | None:
        """Index of the next valid header at/after `start`, or None."""
        b = self.buf
        i = start
        while True:
            j = b.find(START_BYTE, i)
            if j < 0 or j > len(b) - 6:
                return None
            if self._header_at(b, j):
                return j
            i = j + 1

    def next(self) -> Frame | None:
        b = self.buf
        # resync: drop bytes until a valid header sits at position 0
        while b:
            if b[0] != START_BYTE:
                del b[0]
                continue
            if len(b) < 6:
                return None              # maybe a header start; need more bytes
            if self._header_at(b, 0):
                break
            del b[0]                     # a stray 0xff that isn't a header
        if len(b) < 6 or not self._header_at(b, 0):
            return None
        msg_type, version = b[3], b[5]
        size = (b[1] << 8) | b[2]
        end = self._next_header(6)       # real frame end (declared size is unreliable)
        if end is None:
            if len(b) >= self._MAX_FRAME:
                end = min(size, len(b)) if size >= 6 else len(b)   # runaway guard
            else:
                return None              # wait for the next header to delimit us
        payload = bytes(b[6:end])
        del b[:end]
        return Frame(msg_type, version, payload)


# --------------------------------------------------------------------------
# Semantic decoders
# --------------------------------------------------------------------------

def decode_sync(payload: bytes) -> dict[str, Any] | None:
    """Decode a MSG_SYNC payload (device identity). Layout per firmware source;
    v18 appends extra trailing bytes which we ignore."""
    if len(payload) < 20:
        return None
    chip_id = int(struct.unpack_from(">I", payload, 0)[0])
    unique_id = payload[4:12]
    firmware_type = payload[12]
    firmware_version = payload[13]
    build_number = int(struct.unpack_from(">i", payload, 14)[0])
    flags = payload[18]
    num_ranges = payload[19]
    # voltage range calibration table (FScope etc.): per range 9 bytes:
    # channel_and_range(1) | min_uV(int32 BE) | max_uV(int32 BE)
    ranges: dict[tuple[int, int], tuple[float, float]] = {}
    i = 20
    for _ in range(num_ranges):
        if i + 9 > len(payload):
            break
        cr = payload[i]
        ch, rid = cr >> 4, cr & 0x0F
        min_uv = int(struct.unpack_from(">i", payload, i + 1)[0])
        max_uv = int(struct.unpack_from(">i", payload, i + 5)[0])
        ranges[(ch, rid)] = (min_uv / 1e6, max_uv / 1e6)   # volts
        i += 9
    return {
        "chip_id": chip_id,
        "unique_id": unique_id.hex(),
        "firmware_type": firmware_type,
        "firmware_version": firmware_version,
        "build_number": build_number,
        "auto_voltage_range": bool(flags & 0x01),
        "num_voltage_ranges": num_ranges,
        "voltage_ranges": {f"{c},{r}": v for (c, r), v in ranges.items()},
        "extra_bytes": len(payload) - 20,
    }


def build_voltage_range_changed(channel: int, range_id: int) -> bytes:
    """MSG_VOLTAGE_RANGE_CHANGED payload: channel(1), range_id(1)."""
    return bytes([channel & 0xFF, range_id & 0xFF])


def decode_samples(payload: bytes) -> dict[str, Any] | None:
    """Decode a MSG_SAMPLES payload into per-channel 8-bit sample lists.

    Header: flags(1), num_data_channels(1), [chan_id | range<<4]*n,
            real_sample_rate_hz(4 BE), trigger_idx(4 BE signed),
            then interleaved 8-bit samples (one byte per channel per point).

    The SAMPLES channel byte packs channel in the LOW nibble (bits 0-2), range in
    the HIGH nibble (bits 4-5) — `ch_id | (range<<4)`, exactly as the GPL firmware
    documents. Confirmed on hardware: two channels both on range 0
    send bytes [0x00, 0x01] (= ch0, ch1). NB this is the OPPOSITE order from the
    SYNC calibration table (parse_sync_response uses channel<<4|range) — the two
    messages genuinely differ, so don't unify them.
    """
    if len(payload) < 2:
        return None
    flags = payload[0]
    n = payload[1]
    i = 2
    chans: list[dict[str, Any]] = []
    for _ in range(n):
        cb = payload[i]
        i += 1
        chans.append({"id": cb & 0x0F, "voltage_range": (cb >> 4) & 0x0F})
    sample_rate = int(struct.unpack_from(">I", payload, i)[0])
    i += 4
    trigger_idx = int(struct.unpack_from(">i", payload, i)[0])
    i += 4
    raw = payload[i:]
    is_logic = bool(flags & 0x10)
    result: dict[str, Any] = {
        "flags": flags,
        "new_record": bool(flags & 0x01),
        "last_in_frame": bool(flags & 0x02),
        "continuous": bool(flags & 0x04),
        "single_shot": bool(flags & 0x08),
        "logic_mode": is_logic,
        "sample_rate_hz": sample_rate,
        "trigger_idx": trigger_idx,
        "channels": chans,
    }
    if is_logic or not chans:
        result["logic"] = list(raw)
        return result
    # de-interleave: bytes are point0[ch0,ch1,...], point1[...], ...
    nch = len(chans)
    for k, ch in enumerate(chans):
        ch["samples"] = list(raw[k::nch])
    return result


def adc_to_volts(sample: float, vref: float = ADC_VREF) -> float:
    return sample / ADC_FULL_SCALE * vref
