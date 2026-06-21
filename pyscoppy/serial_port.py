"""Minimal dependency-free serial port wrapper (POSIX/Linux).

Scoppy exposes a plain USB CDC ACM device (e.g. /dev/ttyACM0). Baud rate is
irrelevant for USB CDC, so we just put the tty into raw mode and do byte I/O.
We deliberately avoid pyserial so the core driver runs with stdlib only.
"""

import fcntl
import glob
import os
import select
import struct
import termios
import time
import tty

# ioctl numbers for modem control lines (Linux)
TIOCMGET = 0x5415
TIOCMSET = 0x5418
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004

# Raspberry Pi Pico / Scoppy USB vendor:product
SCOPPY_USB_VID = "2e8a"


def _tty_vid(name):
    """USB idVendor for a /dev/ttyACMx node, or None (walks /sys)."""
    base = "/sys/class/tty/%s/device" % name
    for _ in range(6):                       # climb to the USB device dir
        vid = os.path.join(base, "idVendor")
        if os.path.exists(vid):
            try:
                with open(vid) as f:
                    return f.read().strip()
            except OSError:
                return None
        base = os.path.join(base, "..")
    return None


def find_port(preferred="/dev/ttyACM0"):
    """Locate the Scoppy serial node. Prefers `preferred`, else the first
    /dev/ttyACM* whose USB vendor is the Pico's, else the first ACM device.

    Handles the node moving (ACM0 -> ACM1) after a replug.
    """
    nodes = sorted(glob.glob("/dev/ttyACM*"))
    if preferred in nodes:
        return preferred
    for n in nodes:
        if _tty_vid(os.path.basename(n)) == SCOPPY_USB_VID:
            return n
    return nodes[0] if nodes else preferred


class SerialPort:
    def __init__(self, path="/dev/ttyACM0"):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            # CLOCAL: ignore modem control lines (DCD) so writes aren't gated;
            # this is essential for USB CDC ACM or host->device writes never flush.
            # CREAD: enable receiver.
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            # Assert DTR + RTS (some CDC stacks gate I/O on these).
            self._set_modem_bits(TIOCM_DTR | TIOCM_RTS)
            self.flush_input()
        except Exception:
            os.close(self.fd)
            raise

    def pulse_dtr(self, low_s=0.3, settle_s=0.4):
        """Drop then re-assert DTR/RTS. The Scoppy firmware treats this as the app
        disconnecting+reconnecting, so it re-broadcasts SYNC (which carries the voltage
        calibration). Needed when we attach to a device that's already streaming and
        would otherwise never send a fresh SYNC."""
        try:
            cur = struct.unpack("I", fcntl.ioctl(self.fd, TIOCMGET, struct.pack("I", 0)))[0]
            fcntl.ioctl(self.fd, TIOCMSET, struct.pack("I", cur & ~TIOCM_DTR & ~TIOCM_RTS))
            time.sleep(low_s)
            fcntl.ioctl(self.fd, TIOCMSET, struct.pack("I", cur | TIOCM_DTR | TIOCM_RTS))
            time.sleep(settle_s)
            self.flush_input()
        except Exception:
            pass

    def _set_modem_bits(self, bits):
        try:
            cur = struct.unpack("I", fcntl.ioctl(self.fd, TIOCMGET, struct.pack("I", 0)))[0]
            cur |= bits
            fcntl.ioctl(self.fd, TIOCMSET, struct.pack("I", cur))
        except Exception:
            pass

    def flush_input(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except Exception:
            pass

    def read(self, n, timeout=1.0):
        """Read up to n bytes, blocking up to `timeout` seconds total.

        Returns whatever bytes arrived (possibly fewer than n, possibly empty).
        """
        buf = bytearray()
        end = _now() + timeout
        while len(buf) < n:
            remaining = end - _now()
            if remaining <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], remaining)
            if not r:
                break
            try:
                chunk = os.read(self.fd, n - len(buf))
            except BlockingIOError:
                continue
            if chunk:
                buf += chunk
        return bytes(buf)

    def read_exact(self, n, timeout=1.0):
        """Read exactly n bytes or raise TimeoutError."""
        buf = bytearray()
        end = _now() + timeout
        while len(buf) < n:
            remaining = end - _now()
            if remaining <= 0:
                raise TimeoutError(f"wanted {n} bytes, got {len(buf)}")
            r, _, _ = select.select([self.fd], [], [], remaining)
            if not r:
                continue
            try:
                chunk = os.read(self.fd, n - len(buf))
            except BlockingIOError:
                continue
            if chunk:
                buf += chunk
        return bytes(buf)

    def write(self, data, timeout=1.0):
        total = 0
        end = _now() + timeout
        while total < len(data):
            if _now() >= end:
                raise TimeoutError(f"wrote {total}/{len(data)} bytes")
            _, w, _ = select.select([], [self.fd], [], max(0.0, end - _now()))
            if not w:
                continue
            try:
                total += os.write(self.fd, data[total:])
            except BlockingIOError:
                continue
        return total

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _now():
    import time

    return time.monotonic()
