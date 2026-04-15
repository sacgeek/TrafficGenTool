"""
Standalone test runner for the UDP engine and MOS scoring.
No external dependencies — uses unittest + asyncio only.

Run with:
    cd /path/to/netlab
    python3 tests/run_tests.py
"""

import asyncio
import sys
import os
import struct
import time
import random
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.mos import (
    calculate_mos, aggregate_mos, StreamType,
    MOS_EXCELLENT, MOS_GOOD, MOS_FAIR, MOS_POOR,
)
from agent.stream_profiles import PROFILES, VOICE_PROFILE, VIDEO_PROFILE, SCREENSHARE_PROFILE
from agent.workers.udp_sender import UDPSender, HEADER_FORMAT, HEADER_MAGIC, HEADER_SIZE
from agent.workers.udp_receiver import _StreamState


# ---------------------------------------------------------------------------
# MOS engine tests
# ---------------------------------------------------------------------------

class TestMOSCalculation(unittest.TestCase):

    def test_perfect_voice_is_excellent(self):
        r = calculate_mos(0.0, 10.0, 1.0, StreamType.VOICE)
        self.assertGreaterEqual(r.mos, MOS_EXCELLENT)
        self.assertEqual(r.label, "excellent")
        self.assertEqual(r.color, "green")

    def test_perfect_video_is_good_or_better(self):
        r = calculate_mos(0.0, 20.0, 2.0, StreamType.VIDEO)
        self.assertGreaterEqual(r.mos, MOS_GOOD)

    def test_high_latency_degrades_voice(self):
        good = calculate_mos(0.0,  50.0, 2.0, StreamType.VOICE)
        bad  = calculate_mos(0.0, 300.0, 2.0, StreamType.VOICE)
        self.assertGreater(good.mos, bad.mos)

    def test_screenshare_latency_weight_is_lower(self):
        # Screenshare latency_weight=0.5 vs voice latency_weight=1.0
        # At very high latency (800ms) the latency_weight difference should dominate
        # over screenshare's slightly higher ie baseline (8 vs 0).
        voice  = calculate_mos(0.0, 800.0, 5.0, StreamType.VOICE)
        screen = calculate_mos(0.0, 800.0, 5.0, StreamType.SCREENSHARE)
        self.assertGreater(screen.mos, voice.mos)

    def test_loss_degrades_monotonically(self):
        m0  = calculate_mos(0.0,  30.0, 3.0, StreamType.VOICE)
        m5  = calculate_mos(5.0,  30.0, 3.0, StreamType.VOICE)
        m20 = calculate_mos(20.0, 30.0, 3.0, StreamType.VOICE)
        self.assertGreater(m0.mos, m5.mos)
        self.assertGreater(m5.mos, m20.mos)

    def test_jitter_degrades_score(self):
        lo = calculate_mos(0.0, 30.0,  5.0, StreamType.VOICE)
        hi = calculate_mos(0.0, 30.0, 80.0, StreamType.VOICE)
        self.assertGreater(lo.mos, hi.mos)

    def test_worst_case_is_bad(self):
        r = calculate_mos(50.0, 500.0, 200.0, StreamType.VOICE)
        self.assertEqual(r.label, "bad")
        self.assertLessEqual(r.mos, 2.0)

    def test_mos_always_in_valid_range(self):
        for loss in [0, 1, 5, 15, 50, 100]:
            for lat in [0, 50, 150, 400, 800]:
                for jit in [0, 10, 50, 150]:
                    for st in StreamType:
                        r = calculate_mos(loss, lat, jit, st)
                        self.assertGreaterEqual(r.mos, 1.0,
                            f"loss={loss} lat={lat} jit={jit} type={st}")
                        self.assertLessEqual(r.mos, 5.0,
                            f"loss={loss} lat={lat} jit={jit} type={st}")

    def test_r_factor_in_range(self):
        r = calculate_mos(0.0, 20.0, 1.0, StreamType.VOICE)
        self.assertGreaterEqual(r.r_factor, 0.0)
        self.assertLessEqual(r.r_factor, 100.0)

    def test_negative_loss_clamped_to_zero(self):
        r = calculate_mos(-5.0, 30.0, 5.0, StreamType.VOICE)
        self.assertEqual(r.loss_pct, 0.0)

    def test_excess_loss_clamped_to_100(self):
        r = calculate_mos(150.0, 30.0, 5.0, StreamType.VOICE)
        self.assertEqual(r.loss_pct, 100.0)

    def test_to_dict_has_all_keys(self):
        r = calculate_mos(1.0, 50.0, 5.0, StreamType.VIDEO)
        d = r.to_dict()
        for k in ("mos", "r_factor", "label", "color", "loss_pct",
                  "latency_ms", "jitter_ms", "stream_type"):
            self.assertIn(k, d)

    def test_aggregate_empty_returns_none(self):
        self.assertIsNone(aggregate_mos([]))

    def test_aggregate_single(self):
        r   = calculate_mos(0.0, 20.0, 2.0, StreamType.VOICE)
        agg = aggregate_mos([r])
        self.assertIsNotNone(agg)
        self.assertAlmostEqual(agg.mos, r.mos, delta=0.15)

    def test_aggregate_multiple_in_range(self):
        results = [
            calculate_mos(0.0, 20.0, 2.0, StreamType.VOICE),
            calculate_mos(5.0, 80.0, 15.0, StreamType.VOICE),
            calculate_mos(2.0, 40.0, 8.0, StreamType.VOICE),
        ]
        agg = aggregate_mos(results)
        self.assertIsNotNone(agg)
        self.assertGreaterEqual(agg.mos, 1.0)
        self.assertLessEqual(agg.mos, 5.0)


# ---------------------------------------------------------------------------
# Stream profile tests
# ---------------------------------------------------------------------------

class TestStreamProfiles(unittest.TestCase):

    def test_teams_ports(self):
        self.assertEqual(VOICE_PROFILE.port,       3478)
        self.assertEqual(VIDEO_PROFILE.port,       3479)
        self.assertEqual(SCREENSHARE_PROFILE.port, 3480)

    def test_voice_bitrate_range(self):
        self.assertGreater(VOICE_PROFILE.bitrate_kbps, 50)
        self.assertLess(VOICE_PROFILE.bitrate_kbps, 120)

    def test_video_bitrate_range(self):
        self.assertGreater(VIDEO_PROFILE.bitrate_kbps, 200)
        self.assertLess(VIDEO_PROFILE.bitrate_kbps, 600)

    def test_all_stream_types_have_profiles(self):
        for st in StreamType:
            self.assertIn(st, PROFILES)

    def test_pps_matches_interval(self):
        for p in PROFILES.values():
            expected = 1000.0 / p.interval_ms
            self.assertAlmostEqual(p.packets_per_second, expected, places=2)

    def test_dscp_values_are_set(self):
        self.assertEqual(VOICE_PROFILE.dscp, 46)       # EF
        self.assertEqual(VIDEO_PROFILE.dscp, 34)       # AF41
        self.assertEqual(SCREENSHARE_PROFILE.dscp, 18) # AF21


# ---------------------------------------------------------------------------
# Packet construction tests (no network)
# ---------------------------------------------------------------------------

class TestPacketConstruction(unittest.TestCase):

    def test_voice_packet_size(self):
        sender = UDPSender(1, StreamType.VOICE, "127.0.0.1", "127.0.0.1")
        pkt = sender._build_packet(0)
        self.assertEqual(len(pkt), VOICE_PROFILE.packet_size_bytes)

    def test_video_packet_size(self):
        sender = UDPSender(2, StreamType.VIDEO, "127.0.0.1", "127.0.0.1")
        pkt = sender._build_packet(0)
        self.assertEqual(len(pkt), VIDEO_PROFILE.packet_size_bytes)

    def test_header_fields(self):
        sender = UDPSender(stream_id=42, stream_type=StreamType.VIDEO,
                           src_ip="127.0.0.1", dst_ip="127.0.0.1")
        before_us = int(time.time() * 1_000_000)
        pkt = sender._build_packet(seq=99)
        after_us  = int(time.time() * 1_000_000)

        magic, sid, seq, ts = struct.unpack_from(HEADER_FORMAT, pkt)
        self.assertEqual(magic, HEADER_MAGIC)
        self.assertEqual(sid, 42)
        self.assertEqual(seq, 99)
        self.assertGreaterEqual(ts, before_us)
        self.assertLessEqual(ts, after_us + 10_000)

    def test_sequence_increments(self):
        sender = UDPSender(1, StreamType.VOICE, "127.0.0.1", "127.0.0.1")
        seqs = []
        for i in range(5):
            pkt = sender._build_packet(i)
            _, _, seq, _ = struct.unpack_from(HEADER_FORMAT, pkt)
            seqs.append(seq)
        self.assertEqual(seqs, list(range(5)))


# ---------------------------------------------------------------------------
# Receiver state machine tests (no sockets)
# ---------------------------------------------------------------------------

class TestReceiverState(unittest.TestCase):

    def _ts(self):
        return int(time.time() * 1_000_000)

    def test_no_loss_in_order(self):
        state = _StreamState(1, StreamType.VOICE)
        ts = self._ts()
        for seq in range(50):
            state.record_packet(seq, ts)
        loss, _, _ = state.snapshot()
        self.assertAlmostEqual(loss, 0.0, places=1)

    def test_detects_packet_loss(self):
        state = _StreamState(2, StreamType.VOICE)
        ts = self._ts()
        # Send 0–4 and 8–9, skip 5–7 → 3 out of 10 lost = 30%
        for seq in list(range(5)) + list(range(8, 10)):
            state.record_packet(seq, ts)
        loss, _, _ = state.snapshot()
        self.assertAlmostEqual(loss, 30.0, delta=2.0)

    def test_jitter_nonzero_with_variance(self):
        state = _StreamState(3, StreamType.VOICE)
        base_ts = self._ts()
        random.seed(42)
        for seq in range(100):
            offset = int(random.uniform(-50_000, 50_000))
            state.record_packet(seq, base_ts + offset)
        _, _, jitter = state.snapshot()
        self.assertGreater(jitter, 0.0)

    def test_window_reset_after_snapshot(self):
        state = _StreamState(4, StreamType.VOICE)
        ts = self._ts()
        for seq in range(10):
            state.record_packet(seq, ts)
        state.snapshot()  # clears window
        # Send 10 more without loss
        for seq in range(10, 20):
            state.record_packet(seq, ts)
        loss, _, _ = state.snapshot()
        self.assertAlmostEqual(loss, 0.0, places=1)


# ---------------------------------------------------------------------------
# Async integration: loopback voice stream
# ---------------------------------------------------------------------------

class TestLoopbackStream(unittest.IsolatedAsyncioTestCase):

    async def test_loopback_voice_3s(self):
        from agent.workers.udp_session import UDPSession, SessionRole

        snapshots = []
        def collect(snap):
            snapshots.append(snap)

        session = UDPSession(
            session_id  = "loopback-voice-test",
            stream_type = StreamType.VOICE,
            role        = SessionRole.BOTH,
            local_ip    = "127.0.0.1",
            peer_ip     = "127.0.0.1",
            duration_s  = 3.0,
            on_snapshot = collect,
        )

        summary = await session.run()

        self.assertIsNotNone(summary.sender_stats)
        self.assertGreater(summary.sender_stats.packets_sent, 100)
        self.assertGreater(len(snapshots), 0)

        # On loopback: loss should be negligible
        last = snapshots[-1]
        self.assertLess(last.loss_pct, 5.0)

        # MOS on loopback should be at least fair
        self.assertGreaterEqual(last.mos.mos, 3.5)

        print(f"\n  Loopback test: sent={summary.sender_stats.packets_sent} pkts  "
              f"snapshots={len(snapshots)}  "
              f"MOS={last.mos.mos:.2f} ({last.mos.label})  "
              f"loss={last.loss_pct:.1f}%  "
              f"latency={last.latency_ms:.1f}ms  "
              f"jitter={last.jitter_ms:.1f}ms")


if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suite   = unittest.TestSuite()
    for cls in [
        TestMOSCalculation,
        TestStreamProfiles,
        TestPacketConstruction,
        TestReceiverState,
        TestLoopbackStream,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
