"""Unit tests for the wire protocol — pure functions, no hardware needed.

These guard the facts that took real reverse-engineering effort and would be
costly to silently break (see AGENTS.md "Key facts that took real work to find"):

  * the v18 auth token (MD5 of "<nonce+693>Err[45]:9397", first 4 bytes),
  * the SAMPLES channel byte packing channel in the LOW nibble (range in the
    HIGH nibble) — the OPPOSITE order from the SYNC calibration table,
  * the host->Pico frame layout and the headerless Pico->host frame delimiting.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import struct
import unittest
from collections.abc import Sequence

from pyscoppy import protocol as P


def _device_frame(msg_type: int, payload: bytes, version: int = 1) -> bytes:
    """A Pico->host frame (no end byte): START, size(2), type, type+5, version, payload."""
    size = 6 + len(payload)
    return bytes([P.START_BYTE, (size >> 8) & 0xFF, size & 0xFF,
                  msg_type, (msg_type + 5) & 0xFF, version]) + payload


class AuthToken(unittest.TestCase):
    def test_known_answers(self):
        # hard-coded expectations: a wrong-but-self-consistent change still fails
        self.assertEqual(P.compute_auth_token(0).hex(), "ab90dd05")
        self.assertEqual(P.compute_auth_token(1000).hex(), "97aab87e")
        self.assertEqual(P.compute_auth_token(0x01020304).hex(), "8f47a3bc")

    def test_token_is_four_bytes(self):
        self.assertEqual(len(P.compute_auth_token(123)), 4)

    def test_nonce_wraps_at_32_bits(self):
        # (challenge + 693) is taken mod 2**32; huge nonces must not raise
        self.assertEqual(len(P.compute_auth_token(0xFFFFFFFF)), 4)


class SyncNonce(unittest.TestCase):
    def test_reads_big_endian_uint_at_offset_14(self):
        payload = bytearray(20)
        struct.pack_into(">I", payload, P.SYNC_NONCE_OFFSET, 0xDEADBEEF)
        self.assertEqual(P.sync_nonce(bytes(payload)), 0xDEADBEEF)


class SyncResponseV18(unittest.TestCase):
    def test_carries_the_auth_token_at_offset_1(self):
        p = P.build_sync_response_v18(1000, channels=(0, 1))
        self.assertEqual(p[1:5], P.compute_auth_token(1000))
        self.assertEqual(p[1:5].hex(), "97aab87e")

    def test_flags_pack_run_mode_and_app_mode(self):
        scope = P.build_sync_response_v18(0, run_mode=P.STOP, logic_mode=False)
        self.assertEqual(scope[0], P.STOP)                 # run_mode only, app_mode 0
        logic = P.build_sync_response_v18(0, run_mode=P.RUN, logic_mode=True)
        self.assertEqual(logic[0], (1 << 2))               # app_mode 1 shifted into bits 2-3

    def test_channel_bytes_enable_and_disable(self):
        # num_channels at [5], per-channel flags follow; disabled channels must be
        # explicitly turned off (covered up to num_total_channels)
        p = P.build_sync_response_v18(0, channels=(0,), num_total_channels=2)
        self.assertEqual(p[5], 2)
        self.assertEqual(p[6:8], bytes([1, 0]))


class HostFrameEncoding(unittest.TestCase):
    def test_layout_and_size(self):
        frame = P.encode_message(P.MSG_SIG_GEN, b"\xaa\xbb")
        # START, size_hi, size_lo, type, type+5, version, payload..., END
        self.assertEqual(frame[0], P.START_BYTE)
        size = (frame[1] << 8) | frame[2]
        self.assertEqual(size, len(frame))                 # size counts START..END inclusive
        self.assertEqual(frame[3], P.MSG_SIG_GEN)
        self.assertEqual(frame[4], (P.MSG_SIG_GEN + 5) & 0xFF)
        self.assertEqual(frame[5], 1)                      # default version
        self.assertEqual(frame[6:8], b"\xaa\xbb")
        self.assertEqual(frame[-1], P.END_BYTE)


class FrameReaderTests(unittest.TestCase):
    def test_delimits_at_next_header_not_declared_size(self):
        f1 = _device_frame(P.MSG_SYNC, b"hello")
        f2 = _device_frame(P.MSG_SAMPLES, b"world!!")
        f3_header = _device_frame(P.MSG_SAMPLES, b"")      # delimits f2
        r = P.FrameReader()
        r.feed(f1 + f2 + f3_header)
        a = r.next()
        b = r.next()
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        assert a is not None and b is not None             # for the type checker
        self.assertEqual((a.msg_type, a.payload), (P.MSG_SYNC, b"hello"))
        self.assertEqual((b.msg_type, b.payload), (P.MSG_SAMPLES, b"world!!"))

    def test_resyncs_past_leading_garbage(self):
        f1 = _device_frame(P.MSG_SYNC, b"data")
        f2_header = _device_frame(P.MSG_SAMPLES, b"")
        r = P.FrameReader()
        r.feed(b"\x00\x11\xff\x22" + f1 + f2_header)        # junk incl. a stray 0xff
        a = r.next()
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual((a.msg_type, a.payload), (P.MSG_SYNC, b"data"))


class DecodeSamples(unittest.TestCase):
    def _payload(self, flags: int, chan_bytes: Sequence[int], raw: Sequence[int],
                 rate: int = 500_000, trig: int = -1) -> bytes:
        p = bytearray([flags, len(chan_bytes)])
        p += bytes(chan_bytes)
        p += struct.pack(">I", rate)
        p += struct.pack(">i", trig)
        p += bytes(raw)
        return bytes(p)

    def test_channel_in_low_nibble_range_in_high(self):
        # two channels, both range 0 -> bytes [0x00, 0x01] = ch0, ch1 (low nibble)
        raw = [10, 20, 11, 21, 12, 22]                     # interleaved point0,point1,point2
        dec = P.decode_samples(self._payload(0x05, [0x00, 0x01], raw))
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual([c["id"] for c in dec["channels"]], [0, 1])
        self.assertEqual([c["voltage_range"] for c in dec["channels"]], [0, 0])
        self.assertEqual(dec["channels"][0]["samples"], [10, 11, 12])
        self.assertEqual(dec["channels"][1]["samples"], [20, 21, 22])
        self.assertEqual(dec["sample_rate_hz"], 500_000)
        self.assertTrue(dec["new_record"])                 # flags bit0
        self.assertTrue(dec["continuous"])                 # flags bit2

    def test_high_nibble_is_the_range(self):
        dec = P.decode_samples(self._payload(0x01, [0x21], [1, 2, 3]))
        assert dec is not None
        self.assertEqual(dec["channels"][0]["id"], 1)      # low nibble
        self.assertEqual(dec["channels"][0]["voltage_range"], 2)  # high nibble

    def test_logic_mode_returns_raw_bytes(self):
        dec = P.decode_samples(self._payload(0x10, [], [0xAA, 0x55]))
        assert dec is not None
        self.assertTrue(dec["logic_mode"])
        self.assertEqual(dec["logic"], [0xAA, 0x55])


class DecodeSync(unittest.TestCase):
    def test_decodes_identity_and_calibration_table(self):
        p = bytearray()
        p += struct.pack(">I", 0x12345678)                 # chip_id
        p += bytes(range(8))                               # unique_id
        p.append(2)                                        # firmware_type
        p.append(18)                                       # firmware_version
        p += struct.pack(">i", 1000)                       # build_number @ offset 14
        p.append(0x01)                                     # flags: auto_voltage_range
        p.append(1)                                        # num_voltage_ranges
        # one range entry: cr byte 0x02 -> channel (high nibble) 0, range (low) 2
        p.append(0x02)
        p += struct.pack(">i", -6_000_000)                 # min µV
        p += struct.pack(">i", 6_000_000)                  # max µV
        dec = P.decode_sync(bytes(p))
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec["chip_id"], 0x12345678)
        self.assertEqual(dec["firmware_version"], 18)
        self.assertTrue(dec["auto_voltage_range"])
        self.assertEqual(dec["voltage_ranges"], {"0,2": (-6.0, 6.0)})


class NibbleOrdersAreOpposite(unittest.TestCase):
    """The same byte means different things in SAMPLES vs SYNC — must not unify."""

    def test_byte_0x10_means_opposite_things(self):
        # SAMPLES: channel = low nibble, range = high nibble  -> id 0, range 1
        s = P.decode_samples(bytes([0x00, 1, 0x10]) + struct.pack(">I", 0) +
                             struct.pack(">i", 0) + b"\x00")
        assert s is not None
        self.assertEqual((s["channels"][0]["id"], s["channels"][0]["voltage_range"]), (0, 1))
        # SYNC calibration: channel = high nibble, range = low nibble -> ch 1, range 0
        sync = bytearray(20)
        sync[13] = 18
        sync[19] = 1                                        # num_ranges
        sync += bytes([0x10]) + struct.pack(">i", 0) + struct.pack(">i", 0)
        d = P.decode_sync(bytes(sync))
        assert d is not None
        self.assertIn("1,0", d["voltage_ranges"])          # ch 1, range 0


class AdcToVolts(unittest.TestCase):
    def test_endpoints_and_midscale(self):
        self.assertAlmostEqual(P.adc_to_volts(0), 0.0)
        self.assertAlmostEqual(P.adc_to_volts(255), P.ADC_VREF)
        self.assertAlmostEqual(P.adc_to_volts(255, vref=6.6), 6.6)
        self.assertAlmostEqual(P.adc_to_volts(128), 128 / 255 * 3.3)


if __name__ == "__main__":
    unittest.main()
