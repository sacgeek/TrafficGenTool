"""
Test suite for agent/workers/web_worker.py

No real network connections required — HTTP responses are mocked using
unittest.mock so these tests pass in any environment, including CI/CD
pipelines without internet access.

Run with:
    cd /path/to/netlab
    python3 tests/run_web_worker_tests.py

Place this file at:  tests/run_web_worker_tests.py
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.workers.web_worker import (
    WebSnapshot,
    WebWorker,
    _load_to_raw_mos,
    _page_load_mos,
    REPORT_INTERVAL_S,
    _BREAKPOINTS,
)


# ---------------------------------------------------------------------------
# Helper: run coroutine in test
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. MOS scoring — _load_to_raw_mos
# ---------------------------------------------------------------------------

class TestLoadToRawMOS(unittest.TestCase):
    """Verify that load-time → raw MOS mapping respects the defined breakpoints."""

    def test_zero_load_is_perfect(self):
        self.assertEqual(_load_to_raw_mos(0), 5.0)

    def test_exact_breakpoints(self):
        """The function must pass through each (ms, mos) breakpoint exactly."""
        for ms, expected_mos in _BREAKPOINTS:
            result = _load_to_raw_mos(ms)
            self.assertAlmostEqual(
                result, expected_mos, places=5,
                msg=f"Breakpoint ({ms} ms → {expected_mos}) failed: got {result}",
            )

    def test_monotone_decreasing(self):
        """Score must decrease as load time increases."""
        values = [_load_to_raw_mos(ms) for ms in range(0, 11_000, 250)]
        for i in range(len(values) - 1):
            self.assertGreaterEqual(
                values[i], values[i + 1],
                msg=f"Non-monotone at index {i}: {values[i]} < {values[i+1]}",
            )

    def test_very_slow_page_hits_floor(self):
        self.assertAlmostEqual(_load_to_raw_mos(10_000), 1.0, places=2)

    def test_clamped_above_10s(self):
        """Anything beyond 10 000 ms must return 1.0 (floor)."""
        self.assertEqual(_load_to_raw_mos(50_000), 1.0)
        self.assertEqual(_load_to_raw_mos(999_999), 1.0)

    def test_fast_page_excellent(self):
        mos = _load_to_raw_mos(200)
        self.assertGreater(mos, 4.3, "200 ms page should score > excellent threshold")

    def test_mid_range_fair(self):
        mos = _load_to_raw_mos(2_000)
        self.assertGreaterEqual(mos, 3.6)
        self.assertLess(mos, 4.0)


# ---------------------------------------------------------------------------
# 2. MOS scoring — _page_load_mos (with error rate)
# ---------------------------------------------------------------------------

class TestPageLoadMOS(unittest.TestCase):

    def _check(self, load_ms, error_rate, min_mos=None, max_mos=None,
               label=None, color=None):
        mos, lbl, clr = _page_load_mos(load_ms, error_rate)
        if min_mos is not None:
            self.assertGreaterEqual(mos, min_mos, f"load={load_ms} err={error_rate}")
        if max_mos is not None:
            self.assertLessEqual(mos, max_mos, f"load={load_ms} err={error_rate}")
        if label:
            self.assertEqual(lbl, label)
        if color:
            self.assertEqual(clr, color)
        self.assertGreaterEqual(mos, 1.0)
        self.assertLessEqual(mos, 5.0)
        return mos, lbl, clr

    def test_perfect_conditions(self):
        self._check(200, 0.0, min_mos=4.3, label="excellent", color="green")

    def test_acceptable_conditions(self):
        self._check(1_000, 0.0, min_mos=4.0, max_mos=4.3)

    def test_marginal_conditions(self):
        self._check(2_500, 0.1, min_mos=3.0, max_mos=4.0)

    def test_all_requests_failed(self):
        mos, label, _ = _page_load_mos(0.0, 1.0)
        self.assertEqual(mos, 1.0)
        self.assertEqual(label, "bad")

    def test_high_error_rate_degrades_score(self):
        mos_clean, _, _  = _page_load_mos(400, 0.0)
        mos_lossy, _, _  = _page_load_mos(400, 0.5)
        self.assertGreater(mos_clean, mos_lossy)

    def test_slow_with_no_errors_is_poor_or_bad(self):
        mos, lbl, _ = _page_load_mos(7_000, 0.0)
        self.assertLess(mos, 3.1, "7 s load time should be at most poor/bad")

    def test_mos_always_in_range(self):
        for load in [0, 100, 500, 1500, 3000, 6000, 10000, 50000]:
            for err in [0.0, 0.1, 0.5, 0.9, 1.0]:
                mos, lbl, clr = _page_load_mos(load, err)
                self.assertGreaterEqual(mos, 1.0)
                self.assertLessEqual(mos, 5.0)
                self.assertIn(lbl, {"excellent", "good", "fair", "poor", "bad"})
                self.assertIn(clr, {"green", "yellow", "orange", "red"})

    def test_label_color_consistency(self):
        """Label and color must be internally consistent."""
        expected = {
            "excellent": "green",
            "good":      "green",
            "fair":      "yellow",
            "poor":      "orange",
            "bad":       "red",
        }
        for load in [0, 300, 800, 2000, 4000, 8000]:
            _, lbl, clr = _page_load_mos(load, 0.0)
            self.assertEqual(clr, expected[lbl], f"load={load}")


# ---------------------------------------------------------------------------
# 3. WebSnapshot dataclass
# ---------------------------------------------------------------------------

class TestWebSnapshot(unittest.TestCase):

    def _make_snap(self, **kwargs):
        defaults = dict(
            worker_id        = "web-test-u0",
            local_ip         = "192.168.1.101",
            timestamp        = 1_700_000_000.0,
            requests_ok      = 5,
            requests_err     = 1,
            page_load_ms     = 450.0,
            bytes_downloaded = 120_000,
            mos_mos          = 4.1,
            mos_label        = "good",
            mos_color        = "green",
        )
        defaults.update(kwargs)
        return WebSnapshot(**defaults)

    def test_to_dict_has_required_controller_keys(self):
        d = self._make_snap().to_dict()
        for key in (
            "timestamp", "stream_type", "session_id",
            "packets_recv", "packets_expected",
            "loss_pct", "latency_ms", "jitter_ms",
            "mos_mos", "mos_label", "mos_color", "mos_r_factor",
            "page_load_ms", "bytes_downloaded",
        ):
            self.assertIn(key, d, f"Missing key: {key}")

    def test_stream_type_is_web(self):
        d = self._make_snap().to_dict()
        self.assertEqual(d["stream_type"], "web")

    def test_session_id_is_worker_id(self):
        d = self._make_snap(worker_id="web-xyz").to_dict()
        self.assertEqual(d["session_id"], "web-xyz")

    def test_loss_pct_calculation(self):
        snap = self._make_snap(requests_ok=4, requests_err=1)  # 1/5 = 20%
        d = snap.to_dict()
        self.assertAlmostEqual(d["loss_pct"], 20.0, places=1)

    def test_zero_requests_no_division_error(self):
        snap = self._make_snap(requests_ok=0, requests_err=0)
        d = snap.to_dict()
        self.assertEqual(d["loss_pct"], 0.0)
        self.assertEqual(d["packets_expected"], 0)

    def test_latency_ms_mirrors_page_load_ms(self):
        snap = self._make_snap(page_load_ms=750.0)
        d = snap.to_dict()
        self.assertAlmostEqual(d["latency_ms"], 750.0, places=1)
        self.assertAlmostEqual(d["page_load_ms"], 750.0, places=1)

    def test_jitter_and_r_factor_zero(self):
        d = self._make_snap().to_dict()
        self.assertEqual(d["jitter_ms"], 0.0)
        self.assertEqual(d["mos_r_factor"], 0.0)

    def test_bytes_downloaded_passed_through(self):
        d = self._make_snap(bytes_downloaded=999_999).to_dict()
        self.assertEqual(d["bytes_downloaded"], 999_999)


# ---------------------------------------------------------------------------
# 4. WebWorker construction / validation
# ---------------------------------------------------------------------------

class TestWebWorkerConstruction(unittest.TestCase):

    def test_requires_at_least_one_url(self):
        with self.assertRaises(ValueError):
            WebWorker(
                worker_id = "w0",
                local_ip  = "192.168.1.101",
                urls      = [],
            )

    def test_accepts_single_url(self):
        w = WebWorker("w0", "192.168.1.101", ["http://example.com"])
        self.assertEqual(len(w.urls), 1)

    def test_url_cycling(self):
        w = WebWorker("w0", "127.0.0.1", ["http://a.com", "http://b.com"])
        urls = [w._next_url() for _ in range(5)]
        self.assertEqual(urls, [
            "http://a.com", "http://b.com",
            "http://a.com", "http://b.com",
            "http://a.com",
        ])

    def test_default_report_interval(self):
        w = WebWorker("w0", "127.0.0.1", ["http://x.com"])
        self.assertEqual(w.report_interval, REPORT_INTERVAL_S)

    def test_stop_before_run_does_not_raise(self):
        w = WebWorker("w0", "127.0.0.1", ["http://x.com"])
        w.stop()   # should be a no-op before run()
        self.assertTrue(w._stop_event.is_set())


# ---------------------------------------------------------------------------
# 5. WebWorker.run() — mocked aiohttp
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal aiohttp response mock."""
    def __init__(self, body: bytes = b"hello", status: int = 200):
        self._body   = body
        self.status  = status
        self._closed = False

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._closed = True


class _FakeSession:
    """Minimal aiohttp ClientSession mock."""
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.get_calls: list[str] = []

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestWebWorkerRun(unittest.IsolatedAsyncioTestCase):

    async def _run_worker_with_mock(
        self,
        body: bytes     = b"<html>ok</html>",
        status: int     = 200,
        duration_s: float = 0.5,
        n_urls: int     = 2,
        error_on_fetch: Exception | None = None,
    ):
        """Helper: run a WebWorker with mocked aiohttp for duration_s seconds."""
        snapshots: list[WebSnapshot] = []

        def collect(snap):
            snapshots.append(snap)

        worker = WebWorker(
            worker_id       = "test-worker",
            local_ip        = "127.0.0.1",
            urls            = [f"http://host{i}.local/" for i in range(n_urls)],
            duration_s      = duration_s,
            on_snapshot     = collect,
            report_interval = 0.1,   # fast interval for testing
            timeout_s       = 5.0,
        )

        # Patch aiohttp import inside run() and _fetch_page()
        import agent.workers.web_worker as _mod

        fake_resp = _FakeResponse(body=body, status=status)

        if error_on_fetch:
            err = error_on_fetch
            async def _fake_get(url, **kw):
                raise err
            mock_session = MagicMock()
            mock_session.get = _fake_get
        else:
            mock_session = _FakeSession(fake_resp)

        class _FakeConnector:
            async def close(self): pass

        class _FakeClientSession:
            def __init__(self, **kw): pass
            async def __aenter__(self):
                return mock_session
            async def __aexit__(self, *a): pass

        import ssl as _ssl
        fake_ssl_ctx = _ssl.create_default_context()
        fake_ssl_ctx.check_hostname = False
        fake_ssl_ctx.verify_mode = _ssl.CERT_NONE

        mock_aiohttp = MagicMock()
        mock_aiohttp.TCPConnector.return_value     = _FakeConnector()
        mock_aiohttp.ClientSession                 = _FakeClientSession
        mock_aiohttp.ClientTimeout                 = lambda **kw: None

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await worker.run()

        return snapshots, worker

    async def test_emits_snapshots(self):
        """Worker must emit at least one snapshot during its run."""
        snaps, _ = await self._run_worker_with_mock(duration_s=0.4)
        self.assertGreater(len(snaps), 0, "No snapshots emitted")

    async def test_snapshot_has_correct_stream_type(self):
        """Snapshot dicts from to_dict() should identify as 'web'."""
        snaps, _ = await self._run_worker_with_mock(duration_s=0.4)
        for snap in snaps:
            d = snap.to_dict()
            self.assertEqual(d["stream_type"], "web")

    async def test_bytes_downloaded_nonzero(self):
        body = b"x" * 5_000
        snaps, _ = await self._run_worker_with_mock(body=body, duration_s=0.4)
        total_bytes = sum(s.bytes_downloaded for s in snaps)
        self.assertGreater(total_bytes, 0)

    async def test_successful_fetches_tracked(self):
        snaps, _ = await self._run_worker_with_mock(duration_s=0.4)
        total_ok = sum(s.requests_ok for s in snaps)
        self.assertGreater(total_ok, 0)

    async def test_mos_score_in_range(self):
        snaps, _ = await self._run_worker_with_mock(duration_s=0.4)
        for snap in snaps:
            self.assertGreaterEqual(snap.mos_mos, 1.0)
            self.assertLessEqual(snap.mos_mos, 5.0)

    async def test_stop_terminates_early(self):
        """Calling stop() should cause run() to return before duration elapses."""
        worker = WebWorker(
            worker_id   = "stop-test",
            local_ip    = "127.0.0.1",
            urls        = ["http://example.com"],
            duration_s  = 30.0,    # long duration
            report_interval = 0.1,
        )

        import ssl as _ssl
        fake_ssl_ctx = _ssl.create_default_context()
        fake_ssl_ctx.check_hostname = False
        fake_ssl_ctx.verify_mode = _ssl.CERT_NONE

        class _FakeConnector:
            async def close(self): pass

        fake_resp = _FakeResponse(b"ok")
        mock_session = _FakeSession(fake_resp)

        class _FakeClientSession:
            def __init__(self, **kw): pass
            async def __aenter__(self): return mock_session
            async def __aexit__(self, *a): pass

        mock_aiohttp = MagicMock()
        mock_aiohttp.TCPConnector.return_value = _FakeConnector()
        mock_aiohttp.ClientSession             = _FakeClientSession
        mock_aiohttp.ClientTimeout             = lambda **kw: None

        async def _trigger_stop():
            await asyncio.sleep(0.2)
            worker.stop()

        t0 = time.perf_counter()
        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await asyncio.gather(worker.run(), _trigger_stop())
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 5.0, "Worker did not stop promptly after stop()")

    async def test_error_fetches_tracked(self):
        """Network errors should increment requests_err, not crash."""
        class _ErrorSession:
            def get(self, url, **kw):
                raise ConnectionRefusedError("mock: connection refused")
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _FakeConnector:
            async def close(self): pass

        class _FakeClientSession:
            def __init__(self, **kw): pass
            async def __aenter__(self): return _ErrorSession()
            async def __aexit__(self, *a): pass

        snapshots: list[WebSnapshot] = []

        worker = WebWorker(
            worker_id       = "err-worker",
            local_ip        = "127.0.0.1",
            urls            = ["http://unreachable.local/"],
            duration_s      = 0.4,
            on_snapshot     = snapshots.append,
            report_interval = 0.1,
            timeout_s       = 0.2,
        )

        mock_aiohttp = MagicMock()
        mock_aiohttp.TCPConnector.return_value = _FakeConnector()
        mock_aiohttp.ClientSession             = _FakeClientSession
        mock_aiohttp.ClientTimeout             = lambda **kw: None

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await worker.run()

        total_err = sum(s.requests_err for s in snapshots)
        self.assertGreater(total_err, 0, "Error fetches not counted")

        # All snapshots should have MOS ≤ score for 100% error rate
        for snap in snapshots:
            self.assertGreaterEqual(snap.mos_mos, 1.0)
            self.assertLessEqual(snap.mos_mos, 5.0)

    async def test_no_aiohttp_logs_error_and_returns(self):
        """If aiohttp is not installed, run() should log an error and return cleanly."""
        worker = WebWorker(
            worker_id  = "no-aiohttp",
            local_ip   = "127.0.0.1",
            urls       = ["http://x.com"],
            duration_s = 1.0,
        )

        import builtins
        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("aiohttp not installed (mocked)")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_blocked_import):
            # Should return without raising
            await worker.run()


# ---------------------------------------------------------------------------
# 6. Window accumulator reset between snapshots
# ---------------------------------------------------------------------------

class TestWebWorkerWindowReset(unittest.TestCase):
    """Unit-test the _emit_snapshot / window-reset logic directly."""

    def setUp(self):
        self.worker = WebWorker(
            worker_id = "ww",
            local_ip  = "127.0.0.1",
            urls      = ["http://x.com"],
        )
        self.captured: list[WebSnapshot] = []
        self.worker.on_snapshot = self.captured.append

    def test_emit_resets_window(self):
        self.worker._win_load_times = [100.0, 200.0]
        self.worker._win_ok         = 2
        self.worker._win_err        = 0
        self.worker._win_bytes      = 5_000

        self.worker._emit_snapshot()

        self.assertEqual(self.worker._win_ok, 0)
        self.assertEqual(self.worker._win_err, 0)
        self.assertEqual(self.worker._win_bytes, 0)
        self.assertEqual(self.worker._win_load_times, [])

    def test_emit_snapshot_values(self):
        self.worker._win_load_times = [300.0, 700.0]  # mean = 500 ms
        self.worker._win_ok         = 2
        self.worker._win_err        = 0
        self.worker._win_bytes      = 8_192

        self.worker._emit_snapshot()

        self.assertEqual(len(self.captured), 1)
        snap = self.captured[0]
        self.assertAlmostEqual(snap.page_load_ms, 500.0, places=1)
        self.assertEqual(snap.requests_ok, 2)
        self.assertEqual(snap.requests_err, 0)
        self.assertEqual(snap.bytes_downloaded, 8_192)
        self.assertGreater(snap.mos_mos, 4.0,
                           "500 ms mean load should yield a good MOS")

    def test_emit_with_no_data_is_benign(self):
        """Emitting with zero requests should not raise."""
        self.worker._emit_snapshot()
        self.assertEqual(len(self.captured), 1)
        snap = self.captured[0]
        self.assertEqual(snap.requests_ok, 0)
        self.assertEqual(snap.requests_err, 0)
        self.assertEqual(snap.bytes_downloaded, 0)

    def test_emit_with_all_errors(self):
        self.worker._win_load_times = []
        self.worker._win_ok         = 0
        self.worker._win_err        = 5
        self.worker._win_bytes      = 0

        self.worker._emit_snapshot()
        snap = self.captured[0]
        self.assertLessEqual(snap.mos_mos, 2.0,
                             "All-error snapshot should have low MOS")
        self.assertEqual(snap.mos_label, "bad")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    for cls in [
        TestLoadToRawMOS,
        TestPageLoadMOS,
        TestWebSnapshot,
        TestWebWorkerConstruction,
        TestWebWorkerRun,
        TestWebWorkerWindowReset,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
