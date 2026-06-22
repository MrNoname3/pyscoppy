"""scoppyd - a daemon that owns the Pico serial connection and shares it.

Only one host can own /dev/ttyACM0, so this daemon holds a single persistent,
synced connection (LED stays solid) and exposes the live stream + control over a
local Unix socket. Multiple clients (the TUI, and Claude's CLI) connect at once,
all see the same data, and any client's setting changes are applied to the Pico
and broadcast to everyone — so the human and the agent stay in sync.

Stdlib only (select-based). Wire protocol: newline-delimited JSON, see README.
"""

from __future__ import annotations

import json
import os
import select
import socket
import time
from collections import deque
from typing import Any, Optional, TypedDict

from .client import ScoppyClient
from . import protocol as P

DEFAULT_SOCK = "/tmp/scoppyd.sock"
RING = 200_000             # per-channel sample history kept for grab()
DISPLAY_POINTS = 400       # points per channel in a live display frame
FRAME_INTERVAL = 0.1       # seconds between display frames pushed to clients


class SigGen(TypedDict):
    func: int
    freq: int
    duty: int
    gpio: int


class State(TypedDict):
    """The authoritative desired scope state, mirrored to every client."""
    channels: list[int]
    timebase_centi_us: int
    run_mode: int
    trig_mode: int
    trig_channel: int
    trig_type: int
    trig_level: int
    pre_trigger: int
    sample_rate: int
    max_sr: int
    logic_mode: bool
    siggen: SigGen
    vrange: dict[str, int]        # channel id (str) -> active front-end range id
    auto_vrange: dict[str, bool]  # channel id (str) -> auto-range on?
    voltage_ranges: dict[str, Any]  # 'ch,range' -> [min_v, max_v] from the device
    synced: bool
    rate_hz: int


class _Client:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.inbuf = b""
        self.outbuf = b""
        self.subscribed = False
        self.role = "?"

    def queue(self, obj: Any) -> None:
        self.outbuf += (json.dumps(obj) + "\n").encode()


class Daemon:
    def __init__(self, port: str = "/dev/ttyACM0", sock_path: str = DEFAULT_SOCK) -> None:
        self.port = port
        self.sock_path = sock_path
        self.scoppy: Optional[ScoppyClient] = None
        self.connected = False                 # is a device currently open?
        self.last_reconnect = 0.0              # throttle for reconnect attempts
        self.clients: dict[int, _Client] = {}  # fileno -> _Client
        self.rings: dict[int, "deque[int]"] = {}   # channel -> deque
        self.rate = 0
        self.last_frame = 0.0
        self.running = True
        # authoritative desired state (applied to the Pico)
        self.state: State = {
            "channels": [0],
            "timebase_centi_us": 100_000,      # ~500 kS/s
            "run_mode": P.RUN,
            "trig_mode": P.TRIG_NONE,
            "trig_channel": 0,
            "trig_type": P.EDGE_RISING,
            "trig_level": 128,                 # 0..255
            "pre_trigger": 50,                 # pre-trigger samples %, like the app
            "sample_rate": 500_000,            # 500 kS/s default (user finds it clean now).
                                               # NB >~150 kS/s the RP2040 can drop samples,
                                               # which can make the freq read jitter a bit.
            "max_sr": P.MAX_SR_DEFAULT,        # device max-rate code (0=500k,2=1.3M,4=2M,5=2.5M)
            "logic_mode": False,
            "siggen": {"func": P.PWM_SQUARE, "freq": 1000, "duty": 50, "gpio": 255},
            "vrange": {"0": 0, "1": 0},        # active front-end range id per channel
            "auto_vrange": {"0": False, "1": False},  # auto-pick the range per channel
            "voltage_ranges": {},              # calibration table 'ch,range' -> [min_v,max_v]
            "synced": False,
            "rate_hz": 0,
        }
        self.cal: dict[tuple[int, int], tuple[float, float]] = {}  # (ch,range) -> (min_v,max_v)
        self.last_autorange: dict[int, float] = {0: 0.0, 1: 0.0}   # per-channel throttle
        # In buffered mode the device sends disjoint records (a burst sampled at the
        # full rate, then a gap) flagged new_record/last_in_frame. They are NOT
        # contiguous in time, so we must measure/display ONE record at a time rather
        # than concatenate them (concatenating spans the gaps and corrupts timing/freq).
        self.records: dict[int, list[int]] = {}   # ch -> latest complete record
        self._accum: dict[int, list[int]] = {}    # ch -> record being assembled
        self.continuous = False                # device streaming contiguously?
        self._period_pool: dict[int, "deque[int]"] = {}  # ch -> recent rise-to-rise periods
        self.logic_ring: "deque[int]" = deque(maxlen=RING)  # logic bytes (1 byte = 8 channels)

    # -- device ------------------------------------------------------------

    def connect_device(self) -> None:
        # tolerant: the daemon starts (and keeps serving) even with no device yet,
        # and the serve loop reconnects when one appears / after a replug
        if not self._open_device():
            print("scoppyd: no device found yet — will keep trying.", flush=True)

    def _open_device(self) -> bool:
        """Find + open the Pico and sync. Returns True on success."""
        from .serial_port import find_port
        port = find_port(self.port)
        if not os.path.exists(port):
            return False
        try:
            self.scoppy = ScoppyClient(port)
        except OSError:
            self.scoppy = None
            return False
        self.port = port
        self.connected = True
        # Force a fresh SYNC (carries the voltage calibration). If the device was already
        # streaming from a previous session it never sends one on its own, and we'd fall
        # back to the wrong 0–3.3 V scale. A DTR pulse makes it re-broadcast SYNC.
        try:
            self.scoppy.sp.pulse_dtr()
            self.scoppy.read_frame(timeout=4.0, want_type=P.MSG_SYNC)
        except OSError:
            self._handle_disconnect()
            return False
        try:
            self._resync()
        except OSError:
            self._handle_disconnect()
            return False
        return True

    def _handle_disconnect(self) -> None:
        """Device went away (unplug/replug/error): drop it and flag a reconnect."""
        if not self.connected and self.scoppy is None:
            return
        self.connected = False
        self.state["synced"] = False
        if self.scoppy:
            try:
                self.scoppy.close()
            except Exception:
                pass
        self.scoppy = None
        self.last_reconnect = 0.0
        print("scoppyd: device disconnected — reconnecting…", flush=True)
        self._broadcast_state("daemon")

    def _maybe_reconnect(self) -> None:
        """Throttled reconnect attempt while disconnected."""
        now = time.monotonic()
        if now - self.last_reconnect < 1.5:
            return
        self.last_reconnect = now
        if self._open_device():
            print(f"scoppyd: reconnected on {self.port}, "
                  f"synced={self.state['synced']}", flush=True)
            self._broadcast_state("daemon")

    def _resync(self) -> bool:
        if self.scoppy is None:
            self.state["synced"] = False
            return False
        st = self.state
        # how many channels the device has (from the calibration table); used so we can
        # explicitly disable a channel rather than just shortening the list
        n_total = (max(c for (c, _) in self.cal) + 1) if self.cal else 2
        ok = self.scoppy.sync(channels=tuple(st["channels"]),
                              timebase_centi_us=st["timebase_centi_us"],
                              trig_mode=st["trig_mode"], run_mode=st["run_mode"],
                              trig_channel=st["trig_channel"], trig_type=st["trig_type"],
                              trig_level=st["trig_level"], logic_mode=st["logic_mode"],
                              max_sr_code=st["max_sr"], num_total_channels=n_total)
        st["synced"] = ok
        for ch in st["channels"]:
            self.rings.setdefault(ch, deque(maxlen=RING))
        # pick up the FScope (or other) front-end calibration from the SYNC
        vr = self.scoppy.voltage_ranges()
        if vr:
            newcal: dict[tuple[int, int], tuple[float, float]] = {}
            for k, v in vr.items():
                a, b = k.split(",")
                newcal[(int(a), int(b))] = (float(v[0]), float(v[1]))
            self.cal = newcal
            st["voltage_ranges"] = vr
        # a fresh handshake resets the firmware's live params; re-assert ours
        self._apply_device_extras()
        return ok

    def _apply_device_extras(self) -> None:
        """Re-send the live (non-handshake) params the firmware would otherwise
        forget across a resync: selected sample rate, pre-trigger %, vranges."""
        if self.scoppy is None:
            return
        st = self.state
        try:
            self.scoppy.set_sample_rate(int(st["sample_rate"]))
            self.scoppy.set_pre_trigger(int(st["pre_trigger"]))
            for ch, rid in st["vrange"].items():
                self.scoppy.set_voltage_range(int(ch), int(rid))
        except Exception:
            pass

    def _ch_cal(self, ch: int) -> tuple[float, float]:
        """(min_v, max_v) for a channel's active range; default 0..3.3 if none."""
        rid = self.state["vrange"].get(str(ch), 0)
        return self.cal.get((ch, rid), (0.0, P.ADC_VREF))

    def _auto_range(self, ch: int, data: list[int]) -> None:
        """When auto-range is on, pick the front-end range that best fits the recent
        signal: widen (lower id) if it clips; narrow (higher id) only if the signal's
        actual voltage span fits the narrower range with 10% headroom. Working in volts
        (not raw counts) handles off-centre signals; the headroom gives hysteresis."""
        if self.scoppy is None:
            return
        if not self.state["auto_vrange"].get(str(ch)):
            return
        now = time.monotonic()
        if now - self.last_autorange.get(ch, 0.0) < 0.4:
            return
        if not data or len(data) < 200:
            return
        self.last_autorange[ch] = now
        cur = int(self.state["vrange"].get(str(ch), 0))
        cal_cur = self.cal.get((ch, cur))
        if not cal_cur:
            return
        window = data[-1000:]
        lo, hi = min(window), max(window)
        minv, maxv = cal_cur
        v_lo = minv + lo / 255 * (maxv - minv)      # signal voltage extent
        v_hi = minv + hi / 255 * (maxv - minv)
        max_rid = max((r for (c, r) in self.cal if c == ch), default=cur)
        if hi >= 252 or lo <= 3:                    # clipping -> widen (lower id)
            new = cur - 1 if cur > 0 else cur
        elif cur < max_rid:                          # maybe narrow (higher id)
            nxt = self.cal.get((ch, cur + 1))
            if nxt and v_lo > nxt[0] * 0.9 and v_hi < nxt[1] * 0.9:
                new = cur + 1
            else:
                return
        else:
            return
        if new == cur:
            return
        try:
            self.scoppy.set_voltage_range(ch, new)
        except Exception:
            return
        self.state["vrange"][str(ch)] = new
        self._broadcast_state("daemon")

    def _pump_device(self) -> None:
        """Read available SAMPLES from the Pico into the ring buffers."""
        if self.scoppy is None:
            return
        # drain the kernel buffer in a few chunks (500 kS/s is a lot of data)
        for _ in range(16):
            data = self.scoppy.sp.read(16384, timeout=0.02)
            if not data:
                break
            self.scoppy.reader.feed(data)
        while True:
            f = self.scoppy.reader.next()
            if f is None:
                break
            if f.msg_type == P.MSG_SAMPLES:
                dec = P.decode_samples(f.payload)
                if not dec:
                    continue
                # valid samples => we ARE synced (sync() can false-negative when the
                # device is already streaming); reflect reality and notify clients
                if not self.state["synced"]:
                    self.state["synced"] = True
                    self._broadcast_state("daemon")
                self.rate = int(dec["sample_rate_hz"] or self.rate)
                self.state["rate_hz"] = self.rate
                self.continuous = bool(dec["continuous"])
                if dec.get("logic_mode"):
                    # logic-analyzer frame: raw bytes, 1 byte = 8 digital channels
                    # (bit b = channel Db = GP(6+b)). No per-channel de-interleave.
                    logic: list[int] = dec.get("logic") or []
                    self.logic_ring.extend(logic)
                    continue
                # only trust channel ids the calibration knows about; a stray/misframed
                # byte must never create phantom channels or clobber a real vrange.
                valid = {c for (c, _) in self.cal} or {0, 1}
                for ch in dec["channels"]:
                    cid = ch["id"]
                    if cid not in valid:
                        continue
                    self.state["vrange"][str(cid)] = ch.get("voltage_range", 0)
                    if "samples" not in ch:
                        continue
                    samples = ch["samples"]
                    if self.continuous:
                        # contiguous streaming: a rolling ring is fine (no record gaps)
                        ring = self.rings.setdefault(cid, deque(maxlen=RING))
                        ring.extend(samples)
                    else:
                        # buffered: each record is ONE contiguous, device-triggered burst
                        # (e.g. 2048 samples). They are NOT time-contiguous across records,
                        # so we must display ONE record — concatenating them into a ring put
                        # phase jumps at the boundaries (false spikes on screen). Assemble by
                        # new_record/last_in_frame and keep the latest complete record.
                        if dec["new_record"]:
                            self._accum[cid] = list(samples)
                        else:
                            self._accum.setdefault(cid, []).extend(samples)
                        if dec["last_in_frame"]:
                            self.records[cid] = self._accum.get(cid, [])
                            self._accum[cid] = []
                    self._auto_range(cid, samples)
            elif f.msg_type == P.MSG_SYNC:
                # device fell back to unsynced: re-establish
                self.state["synced"] = False
                self._resync()
                self._broadcast_state("daemon")

    # -- display frames ----------------------------------------------------

    def _measure(self, ch: int, data: list[int]) -> dict[str, Any]:
        """Per-channel stats on FULL-resolution samples (not the downsampled display
        points) so Vpp/Vmax/Vmin/Freq are accurate — downsampling for display misses
        the true extremes (e.g. ~9% low Vpp). Values are raw ADC; the client converts
        to volts with the same calibration + probe it uses for the trace.

        Frequency: Schmitt-trigger rising edges (hysteresis rejects midpoint noise), then
        the MEDIAN rise-to-rise period — pooled across recent records so it's robust even
        when one acquisition is short or has a glitch (the true period dominates, outliers
        fall away). Duty is averaged over complete periods of this record."""
        n = len(data)
        lo, hi = min(data), max(data)
        mean = sum(data) / n
        mid = (lo + hi) / 2.0
        amp = hi - lo
        hyst = max(2.0, amp * 0.15)
        hi_th, lo_th = mid + hyst / 2, mid - hyst / 2
        level: Optional[int] = None
        rises: list[int] = []
        falls: list[int] = []
        edges: list[tuple[int, int]] = []   # (index, +1 rising / -1 falling)
        for i, s in enumerate(data):
            if s > hi_th:
                if level == 0:
                    rises.append(i)
                    edges.append((i, 1))
                level = 1
            elif s < lo_th:
                if level == 1:
                    falls.append(i)
                    edges.append((i, -1))
                level = 0
        duty = sum(1 for s in data if s >= mid) / n * 100.0
        # extra measurements: RMS via 2nd moment, edge/pulse counts, min pulse width
        msq = sum(s * s for s in data) / n            # mean of squares (raw counts)
        min_pulse = 0
        pos_pulses = neg_pulses = 0
        if amp > 4 and len(edges) >= 2:
            gaps = [edges[k][0] - edges[k - 1][0] for k in range(1, len(edges))]
            min_pulse = min(gaps)                     # samples; client -> seconds, bit rate
            pos_pulses = sum(1 for k in range(1, len(edges))
                             if edges[k - 1][1] == 1 and edges[k][1] == -1)
            neg_pulses = sum(1 for k in range(1, len(edges))
                             if edges[k - 1][1] == -1 and edges[k][1] == 1)
        # pool rise-to-rise periods across recent records: the median of a few hundred
        # periods is very stable and accurate (each record alone has only a handful).
        pool = self._period_pool.setdefault(ch, deque(maxlen=250))
        if amp > 4 and len(rises) >= 2:
            for k in range(1, len(rises)):
                p = rises[k] - rises[k - 1]
                if p > 0:
                    pool.append(p)
            duties = [sum(1 for s in data[rises[k - 1]:rises[k]] if s >= mid)
                      / (rises[k] - rises[k - 1]) for k in range(1, len(rises))]
            if duties:
                duty = sum(duties) / len(duties) * 100.0
        freq = 0
        if pool and self.rate and amp > 4:
            med = sorted(pool)[len(pool) // 2]
            if med > 0:
                freq = self.rate / med
        return {"min": lo, "max": hi, "mean": mean, "msq": msq, "freq": freq, "duty": duty,
                "pos_edges": len(rises), "neg_edges": len(falls),
                "pos_pulses": pos_pulses, "neg_pulses": neg_pulses, "min_pulse": min_pulse}

    def _win_samples(self) -> int:
        """How many ring samples make up the on-screen window — derived from Time/Div so
        the TIME/DIV control actually zooms. screen_time = timebase_centi_us×1e-7 (10 div),
        samples = screen_time × rate. Bounded so it stays drawable and within the ring."""
        screen_s = self.state["timebase_centi_us"] * 1e-7
        n = int(screen_s * self.rate) if self.rate else DISPLAY_POINTS * 8
        return max(200, min(n, RING))

    def _display_frame(self) -> tuple[dict[int, list[int]], float, dict[int, dict[str, Any]], int]:
        out: dict[int, list[int]] = {}
        meas: dict[int, dict[str, Any]] = {}
        win_s = 0.0
        screen_pts = DISPLAY_POINTS
        n_win = self._win_samples()
        for ch in self.state["channels"]:
            # buffered mode: ONE contiguous device-triggered record (no boundary spikes);
            # continuous mode: the most recent slice of the rolling ring. Either way we send
            # a buffer wider than the on-screen window so the client can trigger-align the
            # slice within the margin without wrapping (the wrap seam used to fake a spike).
            buf: list[int]
            if self.continuous:
                ring = self.rings.get(ch)
                buf = list(ring)[-min(2 * n_win, RING):] if ring else []
            else:
                buf = self.records.get(ch) or []
            if not buf:
                continue
            n_screen = min(n_win, len(buf))                 # on-screen samples (<= record)
            step = max(1, len(buf) // (2 * DISPLAY_POINTS))
            ds = buf[::step]
            out[ch] = ds
            meas[ch] = self._measure(ch, buf)               # measure on the whole contiguous buffer
            if self.rate:
                win_s = n_screen / self.rate
            screen_pts = max(2, min(len(ds), round(len(ds) * n_screen / len(buf))))
        return out, win_s, meas, screen_pts

    def _channel_data(self, ch: int) -> list[int]:
        """Current display/measure window — used by grab too. One contiguous record in
        buffered mode, else the Time/Div window of the rolling ring."""
        if not self.continuous:
            rec = self.records.get(ch)
            return list(rec) if rec else []
        ring = self.rings.get(ch)
        return list(ring)[-self._win_samples():] if ring else []

    def _logic_frame(self) -> tuple[list[int], float]:
        """Downsampled window of logic-analyzer bytes for the display."""
        data = list(self.logic_ring)[-self._win_samples():]
        if not data:
            return [], 0.0
        step = max(1, len(data) // DISPLAY_POINTS)
        win_s = len(data) / self.rate if self.rate else 0.0
        return data[::step][:DISPLAY_POINTS], win_s

    def _push_frames(self) -> None:
        now = time.monotonic()
        if now - self.last_frame < FRAME_INTERVAL:
            return
        self.last_frame = now
        msg: dict[str, Any]
        if self.state["logic_mode"]:
            logic, win_s = self._logic_frame()
            if not logic:
                return
            msg = {"type": "frame", "rate": self.rate, "win_s": win_s,
                   "logic": logic, "logic_mode": True}
        else:
            frame, win_s, meas, screen_pts = self._display_frame()
            if not frame:
                return
            cal = {str(ch): list(self._ch_cal(ch)) for ch in frame}
            msg = {"type": "frame", "rate": self.rate, "win_s": win_s, "screen_pts": screen_pts,
                   "channels": {str(k): v for k, v in frame.items()}, "cal": cal,
                   "meas": {str(k): v for k, v in meas.items()}}
        for c in self.clients.values():
            if c.subscribed:
                c.queue(msg)

    # -- client commands ---------------------------------------------------

    def _broadcast_state(self, by: str) -> None:
        msg = {"type": "state", "state": self.state, "by": by}
        for c in self.clients.values():
            c.queue(msg)

    def _handle_cmd(self, c: _Client, cmd: dict[str, Any]) -> None:
        kind = cmd.get("cmd")
        if kind == "hello":
            c.role = cmd.get("role", "?")
            c.queue({"type": "state", "state": self.state, "by": "daemon"})
        elif kind == "subscribe":
            c.subscribed = True
        elif kind == "unsubscribe":
            c.subscribed = False
        elif kind == "get_state":
            c.queue({"type": "state", "state": self.state, "by": "daemon"})
        elif kind == "grab":
            ch = int(cmd.get("channel", self.state["channels"][0]))
            n = int(cmd.get("n", 2000))
            # return the current single acquisition (records aren't time-contiguous
            # across the gaps, so concatenating them would misrepresent timing)
            data = self._channel_data(ch)[-n:]
            c.queue({"type": "grab", "channel": ch, "rate": self.rate, "data": data})
        elif kind == "set":
            params: dict[str, Any] = cmd.get("params") or {}
            self._apply_set(params, by=c.role)
        elif kind == "siggen":
            self._apply_siggen(cmd, by=c.role)
        elif kind == "reconnect":
            # drop the current device and re-open it (clean re-handshake); also the
            # fix for a device that wandered to a new node or got into a bad state
            self._handle_disconnect()
            self.last_reconnect = 0.0
            self._maybe_reconnect()
        elif kind == "ping":
            c.queue({"type": "pong"})

    def _apply_siggen(self, cmd: dict[str, Any], by: str) -> None:
        sg = self.state["siggen"]
        sg["func"] = int(cmd.get("func", sg["func"]))
        sg["freq"] = int(cmd.get("freq", sg["freq"]))
        sg["duty"] = int(cmd.get("duty", sg["duty"]))
        sg["gpio"] = int(cmd.get("gpio", sg["gpio"]))
        try:
            if self.scoppy:
                self.scoppy.sig_gen_now(func=sg["func"], gpio=sg["gpio"],
                                        freq=sg["freq"], duty=sg["duty"])
        except Exception:
            pass
        self._broadcast_state(by)

    def _apply_set(self, params: dict[str, Any], by: str) -> None:
        st = self.state
        changed = False
        if "channels" in params:
            st["channels"] = sorted(set(int(x) for x in params["channels"])) or [0]
            changed = True
        if "timebase_centi_us" in params:
            # live (type 81) like the app — no blocking re-handshake
            st["timebase_centi_us"] = max(1, int(params["timebase_centi_us"]))
            self._period_pool.clear()          # periods are in samples -> rate-dependent
            try:
                if self.scoppy:
                    self.scoppy.set_horz_scale(st["timebase_centi_us"])
            except Exception:
                pass
            self._broadcast_state(by)
        if "run_mode" in params:
            st["run_mode"] = int(params["run_mode"])
            changed = True
        # trigger mode/type/level/channel all go LIVE (type 83) like the app — a
        # re-handshake here froze the stream for seconds (e.g. switching trig to CH2).
        trig_keys = ("trig_mode", "trig_type", "trig_level", "trig_channel")
        if any(k in params for k in trig_keys):
            if "trig_mode" in params:
                st["trig_mode"] = int(params["trig_mode"])
            if "trig_type" in params:
                st["trig_type"] = int(params["trig_type"])
            if "trig_level" in params:
                st["trig_level"] = max(0, min(255, int(params["trig_level"])))
            if "trig_channel" in params:
                st["trig_channel"] = int(params["trig_channel"])
            try:
                if self.scoppy:
                    self.scoppy.set_trigger(st["trig_mode"], st["trig_channel"],
                                            st["trig_type"], st["trig_level"])
            except Exception:
                pass
            self._broadcast_state(by)
        if "logic_mode" in params:
            st["logic_mode"] = bool(params["logic_mode"])
            self.logic_ring.clear()                # drop stale data across the mode switch
            self.rings.clear()
            changed = True
        if "max_sr" in params:
            # device max-rate ceiling code; it rides in the SYNC_RESPONSE, so resync
            code = int(params["max_sr"])
            st["max_sr"] = code if code in P.MAX_SR_CODES else P.MAX_SR_DEFAULT
            changed = True
        if "sample_rate" in params:
            # selected fixed rate (0 = Auto), sent live like the app: type 85 sets the
            # rate, then a type-81 (current timebase) nudges the firmware to recompute.
            # No blocking re-handshake, so rapid changes never pile up.
            st["sample_rate"] = max(0, int(params["sample_rate"]))
            self._period_pool.clear()          # periods are in samples -> rate-dependent
            try:
                if self.scoppy:
                    self.scoppy.set_sample_rate(st["sample_rate"])
                    self.scoppy.set_horz_scale(st["timebase_centi_us"])
            except Exception:
                pass
            self._broadcast_state(by)
        if "pre_trigger" in params:
            st["pre_trigger"] = max(0, min(100, int(params["pre_trigger"])))
            try:
                if self.scoppy:
                    self.scoppy.set_pre_trigger(st["pre_trigger"])
            except Exception:
                pass
            self._broadcast_state(by)
        if "auto_vrange" in params:
            for ch, on in params["auto_vrange"].items():
                st["auto_vrange"][str(int(ch))] = bool(on)
            self._broadcast_state(by)
        if "vrange" in params:
            # {channel: range_id} -> switch the front-end gain live (no resync).
            # A manual range pick turns OFF auto-range for that channel (like the app).
            for ch, rid in params["vrange"].items():
                ch, rid = int(ch), int(rid)
                try:
                    if self.scoppy:
                        self.scoppy.set_voltage_range(ch, rid)
                except Exception:
                    pass
                st["vrange"][str(ch)] = rid
                st["auto_vrange"][str(ch)] = False
            self._broadcast_state(by)
        if changed:
            # reliable path for v1: re-handshake with the new parameters
            self._resync()
            self._broadcast_state(by)

    # -- socket server -----------------------------------------------------

    def serve(self) -> None:
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        srv.setblocking(False)
        os.chmod(self.sock_path, 0o666)
        print(f"scoppyd: listening on {self.sock_path}, device {self.port}, "
              f"connected={self.connected}, synced={self.state['synced']}", flush=True)
        try:
            while self.running:
                dev_fd = self.scoppy.sp.fd if (self.connected and self.scoppy) else None
                rlist = [srv] + ([dev_fd] if dev_fd is not None else []) \
                    + [c.sock for c in self.clients.values()]
                wlist = [c.sock for c in self.clients.values() if c.outbuf]
                try:
                    r, w, _ = select.select(rlist, wlist, [], FRAME_INTERVAL)
                except (OSError, ValueError):
                    # a fd went bad — most likely the device was unplugged
                    if self.connected and not os.path.exists(self.port):
                        self._handle_disconnect()
                    continue
                if dev_fd is not None and dev_fd in r:
                    try:
                        self._pump_device()
                    except OSError:
                        self._handle_disconnect()
                        continue
                # backstop for a silent unplug (read returns EOF, no error)
                if self.connected and not os.path.exists(self.port):
                    self._handle_disconnect()
                    continue
                if srv in r:
                    self._accept(srv)
                for sock in r:
                    if isinstance(sock, socket.socket) and sock is not srv:
                        self._read_client(sock)
                for sock in w:
                    self._flush_client(sock)
                if not self.connected:
                    self._maybe_reconnect()
                self._push_frames()
        finally:
            srv.close()
            if os.path.exists(self.sock_path):
                os.unlink(self.sock_path)
            if self.scoppy:
                self.scoppy.close()

    def _accept(self, srv: socket.socket) -> None:
        try:
            sock, _ = srv.accept()
        except OSError:
            return
        sock.setblocking(False)
        self.clients[sock.fileno()] = _Client(sock)

    def _read_client(self, sock: socket.socket) -> None:
        c = self.clients.get(sock.fileno())
        if not c:
            return
        try:
            data = sock.recv(65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            self._drop(sock)
            return
        c.inbuf += data
        while b"\n" in c.inbuf:
            line, c.inbuf = c.inbuf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                self._handle_cmd(c, json.loads(line))
            except Exception as e:
                c.queue({"type": "error", "msg": str(e)})

    def _flush_client(self, sock: socket.socket) -> None:
        c = self.clients.get(sock.fileno())
        if not c or not c.outbuf:
            return
        try:
            sent = sock.send(c.outbuf)
            c.outbuf = c.outbuf[sent:]
        except (BlockingIOError, OSError):
            return

    def _drop(self, sock: socket.socket) -> None:
        self.clients.pop(sock.fileno(), None)
        try:
            sock.close()
        except OSError:
            pass


def run(port: str = "/dev/ttyACM0", sock_path: str = DEFAULT_SOCK) -> None:
    d = Daemon(port=port, sock_path=sock_path)
    d.connect_device()
    d.serve()
