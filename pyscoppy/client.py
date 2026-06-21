"""High-level Scoppy client: handshake + sample capture over USB serial."""

import time

from . import protocol as P
from .serial_port import SerialPort


class ScoppyClient:
    def __init__(self, port="/dev/ttyACM0"):
        self.sp = SerialPort(port)
        self.reader = P.FrameReader()
        self.synced = False
        self.last_sync = None
        self.last_sync_payload = None

    # -- low level ---------------------------------------------------------

    def _pump(self, timeout=0.5):
        """Read available bytes into the frame reader."""
        data = self.sp.read(4096, timeout=timeout)
        if data:
            self.reader.feed(data)
        return len(data)

    def read_frame(self, timeout=1.0, want_type=None):
        """Return the next frame (optionally of a given type), or None on timeout."""
        end = time.monotonic() + timeout
        while True:
            f = self.reader.next()
            if f is not None:
                if f.msg_type == P.MSG_SYNC:
                    self.last_sync = P.decode_sync(f.payload)
                    self.last_sync_payload = f.payload
                if want_type is None or f.msg_type == want_type:
                    return f
                continue
            if time.monotonic() >= end:
                return None
            self._pump(timeout=min(0.3, max(0.01, end - time.monotonic())))

    # -- handshake ---------------------------------------------------------

    def read_device_info(self, timeout=3.0):
        """Wait for a SYNC broadcast and return decoded device identity.

        Works without completing the handshake (the Pico broadcasts SYNC while
        waiting for an app)."""
        f = self.read_frame(timeout=timeout, want_type=P.MSG_SYNC)
        if f is None:
            return None
        return P.decode_sync(f.payload)

    def sig_gen_now(self, func=P.PWM_SQUARE, gpio=P.SIG_GEN_DEFAULT_GPIO,
                    freq=1000, duty=50):
        """Send a SIG_GEN (type 84) message immediately (no sync needed)."""
        payload = P.build_sig_gen(func, gpio, freq, duty)
        self.sp.write(P.encode_message(P.MSG_SIG_GEN, payload, version=1), timeout=0.5)

    def set_voltage_range(self, channel, range_id):
        """Switch a channel's analog front-end voltage range (FScope etc.)."""
        payload = P.build_voltage_range_changed(channel, range_id)
        self.sp.write(P.encode_message(P.MSG_VOLTAGE_RANGE_CHANGED, payload, version=1), timeout=0.5)

    def set_sample_rate(self, rate_hz):
        """Select a fixed sample rate (0 = Auto, derive from the timebase).

        Like the app's top-left sample-rate value. Sent live (type 85): the
        firmware just stores it and marks itself dirty, no re-handshake needed.
        """
        payload = P.build_selected_sample_rate(rate_hz)
        self.sp.write(P.encode_message(P.MSG_SELECTED_SAMPLE_RATE, payload, version=1), timeout=0.5)

    def set_trigger(self, trig_mode, trig_channel, trig_type, trig_level):
        """Change trigger mode/channel/type/level live (type 83) — no re-handshake."""
        payload = P.build_trigger_changed(trig_mode, trig_channel, trig_type, trig_level)
        self.sp.write(P.encode_message(P.MSG_TRIGGER_CHANGED, payload, version=1), timeout=0.5)

    def set_horz_scale(self, timebase_centi_us):
        """Change the timebase live (type 81). Forces the firmware to recompute
        the sample rate without a re-handshake."""
        payload = P.build_horz_scale_changed(timebase_centi_us)
        self.sp.write(P.encode_message(P.MSG_HORZ_SCALE_CHANGED, payload, version=1), timeout=0.5)

    def set_pre_trigger(self, percent):
        """Set the pre-trigger samples percentage (0..100). Sent live (type 87)."""
        payload = P.build_pre_trigger_samples(percent)
        self.sp.write(P.encode_message(P.MSG_PRE_TRIGGER_SAMPLES, payload, version=1), timeout=0.5)

    def voltage_ranges(self):
        """Calibration table from the last SYNC: {'ch,range': (min_v, max_v)}."""
        return (self.last_sync or {}).get("voltage_ranges", {})

    def sync(self, channels=(0,), timebase_centi_us=100_000_000,
             trig_mode=P.TRIG_NONE, run_mode=P.RUN, attempts=4,
             trig_channel=0, trig_type=P.EDGE_RISING, trig_level=128,
             logic_mode=False, max_sr_code=P.MAX_SR_DEFAULT, num_total_channels=None):
        """Attempt the v18 SYNC handshake so the Pico starts streaming samples.

        Reads the current SYNC, derives the per-session challenge nonce, computes
        the auth token (MD5 of `(nonce+693)` + salt), and sends a version-3
        SYNC_RESPONSE. Returns True once SAMPLES frames begin to arrive.

        We send gently (paced, few attempts) because flooding the Pico with
        rejected responses can wedge it until it is physically replugged.
        See PROTOCOL.md §"v18 handshake".
        """
        # make sure we have a fresh SYNC payload to read the nonce from
        if self.last_sync_payload is None:
            self.read_frame(timeout=2.0, want_type=P.MSG_SYNC)
        if self.last_sync_payload is None:
            return False
        nonce = P.sync_nonce(self.last_sync_payload)

        for challenge in (nonce, (nonce - 237) & 0xFFFFFFFF):
            resp = P.build_sync_response_v18(
                challenge, channels=channels, run_mode=run_mode,
                timebase_centi_us=timebase_centi_us, trig_mode=trig_mode,
                trig_channel=trig_channel, trig_type=trig_type,
                trig_level=trig_level, logic_mode=logic_mode,
                max_sr_code=max_sr_code, num_total_channels=num_total_channels)
            frame = P.encode_message(
                P.MSG_SYNC_RESPONSE, resp, version=P.SYNC_RESPONSE_VERSION_V18)
            for _ in range(attempts):
                try:
                    self.sp.write(frame, timeout=0.5)
                except TimeoutError:
                    pass
                f = self.read_frame(timeout=1.3)
                if f is not None and f.msg_type == P.MSG_SAMPLES:
                    self.synced = True
                    return True
        return False

    # -- capture -----------------------------------------------------------

    def sig_gen(self, func=P.PWM_SQUARE, gpio=P.SIG_GEN_DEFAULT_GPIO,
                freq=50, duty=50):
        """Start/stop the firmware PWM signal generator (default pin GP22).

        Must be synced first. Send during streaming; the firmware applies it in
        its main loop. Wire GP22 -> GP26 to read it back on channel 0.
        """
        payload = P.build_sig_gen(func, gpio, freq, duty)
        frame = P.encode_message(P.MSG_SIG_GEN, payload, version=1)
        self.sp.write(frame, timeout=0.5)

    def samples(self, timeout=2.0):
        """Generator yielding decoded SAMPLES dicts until `timeout` of silence."""
        while True:
            f = self.read_frame(timeout=timeout, want_type=P.MSG_SAMPLES)
            if f is None:
                return
            dec = P.decode_samples(f.payload)
            if dec is not None:
                yield dec

    def capture(self, channel=0, min_samples=2000, timeout=5.0):
        """Collect at least `min_samples` 8-bit samples for one channel.

        Returns (samples_list, sample_rate_hz).
        """
        out = []
        rate = 0
        end = time.monotonic() + timeout
        for dec in self.samples(timeout=1.0):
            rate = dec["sample_rate_hz"] or rate
            for ch in dec["channels"]:
                if ch["id"] == channel and "samples" in ch:
                    out.extend(ch["samples"])
            if len(out) >= min_samples or time.monotonic() >= end:
                break
        return out, rate

    def close(self):
        self.sp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
