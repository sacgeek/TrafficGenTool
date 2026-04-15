"""
Test suite for agent/workers/youtube_worker.py

No real network or yt-dlp binary required — subprocess calls are mocked via
unittest.mock so the tests run in any environment.

Run with:
    cd /path/to/netlab
    python3 tests/run_youtube_worker_tests.py

Place this file at:  tests/run_youtube_worker_tests.py
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.workers.youtube_worker import (
    YoutubeSnapshot,
    YoutubeWorker,
    QualityProfile,
    QUALITY_PROFILES,
    DEFAULT_QUALITY,
    DEFAULT_URL,
    REPORT_INTERVAL_S,
    STALL_THRESHOLD_RATIO,
    _RATIO_BREAKPOINTS,
    _ratio_to_raw_mos,
    youtube_mos,
    parse_rate_bps,
    _PROGRESS_RE,
    _find_ytdlp,
)


# ---------------------------------------------------------------------------
# Helper: fake async subprocess
# ---------------------------------------------------------------------------

class _FakeStream:
    """Async iterator that yields pre-canned bytes lines."""
    def __init__(self, lines: list[str]):
        self._lines = [l.encode() + b"\n" for l in lines]
        self._idx   = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line


class _FakeProcess:
    """Minimal asyncio.subprocess mock."""
    def __init__(self, stderr_lines: list[str] = None, returncode: int = 0):
        self.stderr     = _FakeStream(stderr_lines or [])
        self.returncode = None           # None = "still running"
        self._rc        = returncode

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.returncode = -9


def _make_fake_create_subprocess(stderr_lines: list[str] = None, returncode: int = 0):
    """Return an async mock that yields a _FakeProcess when awaited."""
    proc = _FakeProcess(stderr_lines, returncode)

    async def _fake(*args, **kwargs):
        return proc

    return _fake, proc


# ---------------------------------------------------------------------------
# 1. Rate parsing
# ---------------------------------------------------------------------------

class TestParseRateBps(unittest.TestCase):

    def test_mib_s(self):
        bps = parse_rate_bps("2.5", "MiB/s")
        self.assertAlmostEqual(bps, 2.5 * 1_048_576, delta=1)

    def test_kib_s(self):
        bps = parse_rate_bps("512.0", "KiB/s")
        self.assertAlmostEqual(bps, 512.0 * 1_024, delta=1)

    def test_gib_s(self):
        bps = parse_rate_bps("1.0", "GiB/s")
        self.assertAlmostEqual(bps, 1_073_741_824, delta=1)

    def test_case_insensitive_mib(self):
        bps = parse_rate_bps("1.0", "mib/s")
        self.assertAlmostEqual(bps, 1_048_576, delta=1)

    def test_zero_rate(self):
        self.assertEqual(parse_rate_bps("0.0", "MiB/s"), 0.0)

    def test_unknown_unit_returns_zero(self):
        self.assertEqual(parse_rate_bps("5.0", "???/s"), 0.0)

    def test_bad_rate_string_returns_zero(self):
        self.assertEqual(parse_rate_bps("N/A", "MiB/s"), 0.0)


# ---------------------------------------------------------------------------
# 2. Progress line regex
# ---------------------------------------------------------------------------

class TestProgressRegex(unittest.TestCase):

    def _parse(self, line: str):
        m = _PROGRESS_RE.search(line)
        if not m:
            return None
        return parse_rate_bps(m.group("rate"), m.group("unit"))

    def test_standard_mib_line(self):
        line = "[download]  14.2% of ~ 89.47MiB at  2.34MiB/s ETA 00:42"
        bps  = self._parse(line)
        self.assertIsNotNone(bps)
        self.assertAlmostEqual(bps, 2.34 * 1_048_576, delta=1000)

    def test_kib_line(self):
        line = "[download]   5.1% of ~  1.00GiB at 512.00KiB/s ETA 01:52"
        bps  = self._parse(line)
        self.assertIsNotNone(bps)
        self.assertAlmostEqual(bps, 512 * 1024, delta=100)

    def test_gib_line(self):
        line = "[download]  99.9% of ~  2.50GiB at  1.20GiB/s ETA 00:00"
        bps  = self._parse(line)
        self.assertIsNotNone(bps)
        self.assertAlmostEqual(bps, 1.20 * 1_073_741_824, delta=1000)

    def test_fragment_suffix_ignored(self):
        line = "[download]  14.2% of ~ 89.47MiB at  2.34MiB/s ETA 00:42 (frag 3/8)"
        bps  = self._parse(line)
        self.assertIsNotNone(bps)

    def test_non_download_line_returns_none(self):
        self.assertIsNone(self._parse("[info] Available formats for xxx: ..."))
        self.assertIsNone(self._parse("[youtube] Extracting URL: https://..."))

    def test_100_percent_line(self):
        line = "[download] 100% of ~ 89.47MiB at   5.12MiB/s ETA 00:00"
        bps  = self._parse(line)
        self.assertIsNotNone(bps)
        self.assertGreater(bps, 0)

    def test_various_percentage_formats(self):
        for pct in ["  0.0", " 14.2", "100"]:
            line = f"[download] {pct}% of ~ 50MiB at 1.00MiB/s ETA 00:10"
            self.assertIsNotNone(self._parse(line), f"Failed for pct={pct!r}")


# ---------------------------------------------------------------------------
# 3. Throughput-ratio → raw MOS
# ---------------------------------------------------------------------------

class TestRatioToRawMOS(unittest.TestCase):

    def test_zero_ratio_is_bad(self):
        self.assertEqual(_ratio_to_raw_mos(0.0), 1.0)

    def test_negative_ratio_is_bad(self):
        self.assertEqual(_ratio_to_raw_mos(-1.0), 1.0)

    def test_exact_breakpoints(self):
        for ratio, expected in _RATIO_BREAKPOINTS:
            result = _ratio_to_raw_mos(ratio)
            self.assertAlmostEqual(
                result, expected, places=5,
                msg=f"Breakpoint ratio={ratio} → expected {expected}, got {result}",
            )

    def test_monotone_increasing(self):
        vals = [_ratio_to_raw_mos(r / 10) for r in range(0, 35)]
        for i in range(len(vals) - 1):
            self.assertLessEqual(
                vals[i], vals[i + 1],
                msg=f"Non-monotone at index {i}: {vals[i]} > {vals[i+1]}",
            )

    def test_above_max_breakpoint(self):
        self.assertAlmostEqual(_ratio_to_raw_mos(10.0), 5.0, places=1)


# ---------------------------------------------------------------------------
# 4. youtube_mos (with stall penalty)
# ---------------------------------------------------------------------------

class TestYoutubeMOS(unittest.TestCase):

    def _check(self, avg_kbps, req_kbps, stall_frac,
               min_mos=None, max_mos=None, label=None, color=None):
        mos, lbl, clr = youtube_mos(avg_kbps, req_kbps, stall_frac)
        if min_mos is not None:
            self.assertGreaterEqual(mos, min_mos)
        if max_mos is not None:
            self.assertLessEqual(mos, max_mos)
        if label:
            self.assertEqual(lbl, label)
        if color:
            self.assertEqual(clr, color)
        self.assertGreaterEqual(mos, 1.0)
        self.assertLessEqual(mos, 5.0)
        return mos, lbl, clr

    def test_excellent_throughput_no_stall(self):
        self._check(9_000, 3_000, 0.0, min_mos=4.3, label="excellent", color="green")

    def test_exactly_required_bitrate_is_fair(self):
        mos, lbl, _ = youtube_mos(3_000, 3_000, 0.0)
        self.assertAlmostEqual(mos, 3.6, places=1)
        self.assertEqual(lbl, "fair")

    def test_half_required_is_poor(self):
        mos, lbl, _ = youtube_mos(1_500, 3_000, 0.0)
        self.assertLessEqual(mos, 3.1)

    def test_zero_throughput_is_bad(self):
        mos, lbl, _ = youtube_mos(0, 3_000, 0.0)
        self.assertEqual(lbl, "bad")
        self.assertEqual(mos, 1.0)

    def test_stall_penalty_degrades_score(self):
        mos_clean, _, _ = youtube_mos(6_000, 3_000, 0.0)
        mos_stall, _, _ = youtube_mos(6_000, 3_000, 0.5)
        self.assertGreater(mos_clean, mos_stall)

    def test_full_stall_window_heavy_penalty(self):
        mos_clean, _, _ = youtube_mos(6_000, 3_000, 0.0)
        mos_full,  _, _ = youtube_mos(6_000, 3_000, 1.0)
        self.assertGreater(mos_clean - mos_full, 1.0,
                           "Full stall should drop MOS by more than 1 point")

    def test_mos_always_in_valid_range(self):
        for kbps in [0, 500, 1500, 3000, 6000, 12000]:
            for req in [500, 1500, 3000, 8000]:
                for sf in [0.0, 0.25, 0.5, 1.0]:
                    mos, lbl, clr = youtube_mos(kbps, req, sf)
                    self.assertGreaterEqual(mos, 1.0)
                    self.assertLessEqual(mos, 5.0)
                    self.assertIn(lbl, {"excellent", "good", "fair", "poor", "bad"})
                    self.assertIn(clr, {"green", "yellow", "orange", "red"})

    def test_label_color_consistency(self):
        expected_color = {
            "excellent": "green",
            "good":      "green",
            "fair":      "yellow",
            "poor":      "orange",
            "bad":       "red",
        }
        for kbps in [0, 1500, 3000, 6000, 9000]:
            _, lbl, clr = youtube_mos(kbps, 3_000, 0.0)
            self.assertEqual(clr, expected_color[lbl], f"kbps={kbps}")


# ---------------------------------------------------------------------------
# 5. Quality profiles
# ---------------------------------------------------------------------------

class TestQualityProfiles(unittest.TestCase):

    def test_all_standard_qualities_present(self):
        for q in ("360p", "480p", "720p", "1080p"):
            self.assertIn(q, QUALITY_PROFILES)

    def test_min_kbps_increases_with_quality(self):
        tiers = ["360p", "480p", "720p", "1080p"]
        kbps  = [QUALITY_PROFILES[q].min_kbps for q in tiers]
        for i in range(len(kbps) - 1):
            self.assertLess(kbps[i], kbps[i + 1])

    def test_yt_format_strings_non_empty(self):
        for q, profile in QUALITY_PROFILES.items():
            self.assertTrue(profile.yt_format, f"Empty yt_format for {q}")

    def test_default_quality_exists(self):
        self.assertIn(DEFAULT_QUALITY, QUALITY_PROFILES)


# ---------------------------------------------------------------------------
# 6. YoutubeSnapshot dataclass
# ---------------------------------------------------------------------------

class TestYoutubeSnapshot(unittest.TestCase):

    def _make_snap(self, **kw):
        defaults = dict(
            worker_id        = "yt-test-u0",
            local_ip         = "192.168.1.101",
            timestamp        = 1_700_000_000.0,
            quality          = "720p",
            throughput_kbps  = 5_000.0,
            bytes_downloaded = 1_250_000,
            stall_seconds    = 0.0,
            mos_mos          = 4.2,
            mos_label        = "good",
            mos_color        = "green",
        )
        defaults.update(kw)
        return YoutubeSnapshot(**defaults)

    def test_to_dict_has_required_controller_keys(self):
        d = self._make_snap().to_dict()
        for key in (
            "timestamp", "stream_type", "session_id",
            "packets_recv", "packets_expected",
            "loss_pct", "latency_ms", "jitter_ms",
            "mos_mos", "mos_label", "mos_color", "mos_r_factor",
            "bytes_downloaded",
        ):
            self.assertIn(key, d, f"Missing key: {key}")

    def test_stream_type_is_youtube(self):
        d = self._make_snap().to_dict()
        self.assertEqual(d["stream_type"], "youtube")

    def test_session_id_is_worker_id(self):
        d = self._make_snap(worker_id="yt-xyz").to_dict()
        self.assertEqual(d["session_id"], "yt-xyz")

    def test_stall_expressed_as_loss_pct(self):
        # 1 second stall in a 2-second window = 50%
        snap = self._make_snap(stall_seconds=1.0)
        d    = snap.to_dict()
        self.assertAlmostEqual(d["loss_pct"], 50.0, delta=2.0)

    def test_no_stall_means_zero_loss(self):
        d = self._make_snap(stall_seconds=0.0).to_dict()
        self.assertEqual(d["loss_pct"], 0.0)

    def test_youtube_specific_fields_present(self):
        d = self._make_snap(quality="1080p", throughput_kbps=10_000.0).to_dict()
        self.assertEqual(d["quality"], "1080p")
        self.assertAlmostEqual(d["throughput_kbps"], 10_000.0, delta=1)

    def test_bytes_downloaded_passed_through(self):
        d = self._make_snap(bytes_downloaded=999_888).to_dict()
        self.assertEqual(d["bytes_downloaded"], 999_888)


# ---------------------------------------------------------------------------
# 7. YoutubeWorker construction
# ---------------------------------------------------------------------------

class TestYoutubeWorkerConstruction(unittest.TestCase):

    def test_valid_quality_accepted(self):
        for q in QUALITY_PROFILES:
            w = YoutubeWorker("w", "127.0.0.1", "http://x.com", quality=q)
            self.assertEqual(w.quality, q)

    def test_invalid_quality_raises(self):
        with self.assertRaises(ValueError):
            YoutubeWorker("w", "127.0.0.1", "http://x.com", quality="8k")

    def test_default_quality_is_720p(self):
        w = YoutubeWorker("w", "127.0.0.1", "http://x.com")
        self.assertEqual(w.quality, "720p")

    def test_stop_before_run_is_harmless(self):
        w = YoutubeWorker("w", "127.0.0.1", "http://x.com")
        w.stop()
        self.assertTrue(w._stop_event.is_set())

    def test_profile_min_kbps_matches_quality(self):
        for q, profile in QUALITY_PROFILES.items():
            w = YoutubeWorker("w", "127.0.0.1", "http://x.com", quality=q)
            self.assertEqual(w.profile.min_kbps, profile.min_kbps)


# ---------------------------------------------------------------------------
# 8. _parse_progress_line (unit test on the method directly)
# ---------------------------------------------------------------------------

class TestParseProgressLine(unittest.TestCase):

    def setUp(self):
        self.worker = YoutubeWorker(
            "w", "127.0.0.1", "http://x.com",
            quality="720p", report_interval=2.0,
        )
        # Give a fake last_rate_ts so dt calculations work
        self.worker._last_rate_ts = time.time() - 0.5

    def test_parses_mib_line(self):
        self.worker._parse_progress_line(
            "[download]  50.0% of ~ 89.47MiB at  3.00MiB/s ETA 00:20"
        )
        self.assertEqual(len(self.worker._win_rates_bps), 1)
        rate = self.worker._win_rates_bps[0]
        self.assertAlmostEqual(rate, 3.0 * 1_048_576, delta=1000)

    def test_ignores_non_download_lines(self):
        self.worker._parse_progress_line("[info] Some metadata line")
        self.worker._parse_progress_line("[youtube] Extracting URL")
        self.assertEqual(len(self.worker._win_rates_bps), 0)

    def test_stall_detected_when_below_threshold(self):
        # For 720p: min_kbps=3000, stall threshold = 3000*1000/8*0.8 = 300,000 B/s = 2400 kbps
        # 50 KiB/s = 51,200 B/s = 409 kbps — clearly below threshold → stall
        self.worker._parse_progress_line(
            "[download]  10.0% of ~ 50MiB at   50.00KiB/s ETA 08:00"
        )
        self.assertGreater(self.worker._win_stall_s, 0.0)

    def test_no_stall_when_above_threshold(self):
        # Rate = 5000 kbps → well above 720p threshold (2400 kbps)
        self.worker._win_stall_s = 0.0
        self.worker._parse_progress_line(
            "[download]  10.0% of ~ 50MiB at  5.00MiB/s ETA 00:10"
        )
        self.assertEqual(self.worker._win_stall_s, 0.0)

    def test_bytes_accumulate(self):
        before = self.worker._win_bytes
        self.worker._parse_progress_line(
            "[download]  10.0% of ~ 50MiB at  2.00MiB/s ETA 00:25"
        )
        self.assertGreaterEqual(self.worker._win_bytes, before)


# ---------------------------------------------------------------------------
# 9. _emit_snapshot (unit test)
# ---------------------------------------------------------------------------

class TestEmitSnapshot(unittest.TestCase):

    def setUp(self):
        self.snaps: list[YoutubeSnapshot] = []
        self.worker = YoutubeWorker(
            "yt-emit-test", "127.0.0.1", "http://x.com",
            quality="720p",
            on_snapshot=self.snaps.append,
        )

    def test_emit_calls_on_snapshot(self):
        self.worker._win_rates_bps = [3_000_000.0]   # 3 MB/s
        self.worker._win_bytes     = 6_000_000
        self.worker._win_stall_s   = 0.0
        self.worker._emit_snapshot()
        self.assertEqual(len(self.snaps), 1)

    def test_emit_resets_window(self):
        self.worker._win_rates_bps = [1_000_000.0]
        self.worker._win_bytes     = 500_000
        self.worker._win_stall_s   = 0.5
        self.worker._emit_snapshot()
        self.assertEqual(self.worker._win_rates_bps, [])
        self.assertEqual(self.worker._win_bytes, 0)
        self.assertEqual(self.worker._win_stall_s, 0.0)

    def test_emit_no_data_is_benign(self):
        self.worker._emit_snapshot()
        self.assertEqual(len(self.snaps), 1)
        snap = self.snaps[0]
        self.assertEqual(snap.throughput_kbps, 0.0)

    def test_emit_mos_in_range(self):
        self.worker._win_rates_bps = [5_000_000.0]  # ~5 MB/s = ~40 Mbps
        self.worker._emit_snapshot()
        snap = self.snaps[0]
        self.assertGreaterEqual(snap.mos_mos, 1.0)
        self.assertLessEqual(snap.mos_mos, 5.0)

    def test_emit_stall_expressed(self):
        self.worker._win_rates_bps = [100_000.0]  # very slow
        self.worker._win_stall_s   = 1.5           # 1.5 s stall in 2 s window
        self.worker._emit_snapshot()
        snap = self.snaps[0]
        self.assertGreater(snap.stall_seconds, 0)
        self.assertLessEqual(snap.mos_mos, 3.1, "Heavy stall should yield poor/bad MOS")


# ---------------------------------------------------------------------------
# 10. YoutubeWorker.run() — mocked subprocess
# ---------------------------------------------------------------------------

class TestYoutubeWorkerRun(unittest.IsolatedAsyncioTestCase):

    def _make_worker(self, duration_s=0.5, quality="720p") -> tuple[YoutubeWorker, list]:
        snaps: list[YoutubeSnapshot] = []
        w = YoutubeWorker(
            worker_id       = "yt-run-test",
            local_ip        = "127.0.0.1",
            url             = "http://fake-stream.local/video",
            quality         = quality,
            duration_s      = duration_s,
            on_snapshot     = snaps.append,
            report_interval = 0.1,
        )
        return w, snaps

    def _fake_subprocess(self, lines: list[str], returncode=0):
        """Return a patched asyncio.create_subprocess_exec + proc."""
        create_fn, proc = _make_fake_create_subprocess(lines, returncode)
        return patch("asyncio.create_subprocess_exec", new=create_fn), proc

    async def test_emits_snapshots_from_parsed_output(self):
        progress_lines = [
            "[download]  10.0% of ~ 50MiB at  3.00MiB/s ETA 00:17",
            "[download]  20.0% of ~ 50MiB at  3.10MiB/s ETA 00:15",
            "[download]  30.0% of ~ 50MiB at  2.90MiB/s ETA 00:14",
        ]
        worker, snaps = self._make_worker(duration_s=0.5)

        patcher, _proc = self._fake_subprocess(progress_lines)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()

        self.assertGreater(len(snaps), 0, "No snapshots emitted")

    async def test_throughput_nonzero_with_good_lines(self):
        lines = [f"[download]  {i}% of ~ 50MiB at  5.00MiB/s ETA 00:10" for i in range(1, 20)]
        worker, snaps = self._make_worker(duration_s=0.5)

        patcher, _ = self._fake_subprocess(lines)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()

        total_bytes = sum(s.bytes_downloaded for s in snaps)
        self.assertGreater(total_bytes, 0)

    async def test_mos_in_valid_range(self):
        lines = ["[download]  50.0% of ~ 50MiB at  4.00MiB/s ETA 00:06"]
        worker, snaps = self._make_worker(duration_s=0.5)

        patcher, _ = self._fake_subprocess(lines)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()

        for snap in snaps:
            self.assertGreaterEqual(snap.mos_mos, 1.0)
            self.assertLessEqual(snap.mos_mos, 5.0)

    async def test_stop_terminates_promptly(self):
        # Many lines so the process would "run" for a while
        lines = ["[download]  1.0% of ~ 500MiB at  1.00MiB/s ETA 08:00"] * 500
        worker, _ = self._make_worker(duration_s=30.0)

        patcher, _ = self._fake_subprocess(lines)

        async def _trigger_stop():
            await asyncio.sleep(0.15)
            worker.stop()

        t0 = time.perf_counter()
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await asyncio.gather(worker.run(), _trigger_stop())
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 5.0, "Worker did not stop promptly")

    async def test_missing_ytdlp_returns_cleanly(self):
        worker, snaps = self._make_worker(duration_s=1.0)
        with patch("agent.workers.youtube_worker._find_ytdlp", return_value=None):
            await worker.run()  # should return without raising
        # No snapshots expected (process never ran)
        self.assertEqual(len(snaps), 0)

    async def test_low_throughput_produces_degraded_mos(self):
        # 100 KiB/s = ~819 kbps — far below 720p minimum (3000 kbps).
        # The fake stream delivers lines with ~zero delay (dt≈0), so stall_seconds
        # won't accumulate much.  Instead, verify that the low average throughput
        # correctly produces a poor/bad MOS — the primary quality indicator.
        lines = [
            "[download]  10.0% of ~ 50MiB at 100.00KiB/s ETA 10:00",
            "[download]  11.0% of ~ 50MiB at 100.00KiB/s ETA 10:00",
            "[download]  12.0% of ~ 50MiB at 100.00KiB/s ETA 10:00",
            "[download]  13.0% of ~ 50MiB at 100.00KiB/s ETA 10:00",
        ]
        worker, snaps = self._make_worker(duration_s=0.5)

        patcher, _ = self._fake_subprocess(lines)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()

        # At least one snapshot must exist
        self.assertGreater(len(snaps), 0, "No snapshots emitted")
        # Every snapshot should reflect the throughput degradation
        for snap in snaps:
            if snap.throughput_kbps > 0:   # skip zero-throughput flush snapshots
                self.assertLessEqual(
                    snap.mos_mos, 3.1,
                    f"100 KiB/s should yield poor/bad MOS, got {snap.mos_mos} ({snap.mos_label})",
                )

    async def test_snapshot_stream_type_is_youtube(self):
        lines = ["[download]  50.0% of ~ 50MiB at  3.00MiB/s ETA 00:06"]
        worker, snaps = self._make_worker(duration_s=0.5)

        patcher, _ = self._fake_subprocess(lines)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()

        for snap in snaps:
            d = snap.to_dict()
            self.assertEqual(d["stream_type"], "youtube")

    async def test_non_zero_return_code_does_not_raise(self):
        """yt-dlp failing (e.g. geo-blocked) should log but not crash the worker."""
        lines = ["ERROR: This video is unavailable."]
        worker, _ = self._make_worker(duration_s=0.3)

        patcher, _ = self._fake_subprocess(lines, returncode=1)
        with patcher:
            with patch("agent.workers.youtube_worker._find_ytdlp", return_value="/usr/bin/yt-dlp"):
                await worker.run()   # should not raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestParseRateBps,
        TestProgressRegex,
        TestRatioToRawMOS,
        TestYoutubeMOS,
        TestQualityProfiles,
        TestYoutubeSnapshot,
        TestYoutubeWorkerConstruction,
        TestParseProgressLine,
        TestEmitSnapshot,
        TestYoutubeWorkerRun,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
