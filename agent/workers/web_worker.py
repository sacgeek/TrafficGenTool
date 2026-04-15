"""
Web worker: headless web browsing simulation.

Each WebWorker represents one simulated user browsing from a specific source IP
alias, cycling through a list of URLs and emitting telemetry every 2 seconds.

Source IP binding is achieved via aiohttp's TCPConnector(local_addr=(ip, 0)),
which calls bind() on each outgoing connection socket.  This means HTTP packets
genuinely originate from the assigned IP alias — visible to switches, firewalls,
and QoS policies as a distinct source address.

Telemetry emitted every REPORT_INTERVAL_S seconds:
    page_load_ms      mean page load time for successful fetches in the window
    bytes_downloaded  cumulative response bytes in the window
    requests_ok       successful fetches
    requests_err      failed fetches (timeout, DNS, connection refused, …)

A synthetic MOS-style quality score (mos_mos: 1.0–5.0) is derived from page
load time and error rate, using the same breakpoints as the dashboard colour
thresholds so that all stream types display consistently.

Usage (from PlanExecutor in agent.py):

    worker = WebWorker(
        worker_id  = "web-plan01-u3",
        local_ip   = "192.168.1.103",
        urls       = ["http://intranet.corp/", "http://172.16.0.1/speedtest"],
        duration_s = 60,
        on_snapshot = agent.on_snapshot_callback,
    )
    task = asyncio.create_task(worker.run())
    # …later…
    worker.stop()
    await task

Place this file at:  agent/workers/web_worker.py
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_INTERVAL_S = 2.0     # telemetry emission cadence (matches UDP workers)
DEFAULT_TIMEOUT_S = 10.0    # per-request hard timeout
MIN_PAUSE_S       = 0.3     # minimum gap between consecutive page fetches

# ---------------------------------------------------------------------------
# MOS scoring — piecewise-linear mapping: load_ms → raw quality score
# ---------------------------------------------------------------------------
#
# Breakpoints chosen to align with the dashboard colour thresholds:
#   < 500 ms           → excellent (≥ 4.3)
#   500 – 1 500 ms     → good      (≥ 4.0)
#   1 500 – 3 000 ms   → fair      (≥ 3.6)
#   3 000 – 6 000 ms   → poor      (≥ 3.1)
#   > 6 000 ms         → bad       (< 3.1)

_BREAKPOINTS: list[tuple[float, float]] = [
    (0,      5.0),
    (500,    4.3),
    (1_500,  4.0),
    (3_000,  3.6),
    (6_000,  3.1),
    (10_000, 1.0),
]


def _load_to_raw_mos(load_ms: float) -> float:
    """Piecewise-linear mapping from page load time (ms) to a raw MOS value."""
    if load_ms <= 0:
        return 5.0
    for i in range(len(_BREAKPOINTS) - 1):
        x0, y0 = _BREAKPOINTS[i]
        x1, y1 = _BREAKPOINTS[i + 1]
        if load_ms <= x1:
            t = (load_ms - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return 1.0


def _page_load_mos(
    avg_load_ms: float,
    error_rate: float,      # 0.0 – 1.0
) -> tuple[float, str, str]:
    """
    Derive a 1.0–5.0 MOS-style score from browsing metrics.

    Returns (mos, label, color).
    """
    if error_rate >= 1.0 and avg_load_ms <= 0:
        # Every request failed, nothing loaded
        mos = 1.0
    else:
        raw = _load_to_raw_mos(avg_load_ms)
        # Error rate penalty: 100 % errors → 90 % reduction in score
        mos = max(1.0, raw * (1.0 - error_rate * 0.9))

    mos = max(1.0, min(5.0, round(mos, 2)))

    if mos >= 4.3:
        label, color = "excellent", "green"
    elif mos >= 4.0:
        label, color = "good", "green"
    elif mos >= 3.6:
        label, color = "fair", "yellow"
    elif mos >= 3.1:
        label, color = "poor", "orange"
    else:
        label, color = "bad", "red"

    return mos, label, color


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------

@dataclass
class WebSnapshot:
    """
    Telemetry report for one web worker window, emitted every REPORT_INTERVAL_S.
    The to_dict() output is wire-compatible with the controller's StreamSnapshot
    Pydantic model (page_load_ms and bytes_downloaded are the web-specific fields;
    the UDP-style fields are zero-filled so the controller doesn't reject the message).
    """
    worker_id:        str
    local_ip:         str
    timestamp:        float

    requests_ok:      int     # successful GETs in this window
    requests_err:     int     # failed GETs in this window
    page_load_ms:     float   # mean response time for successful fetches (ms)
    bytes_downloaded: int     # total response bytes in this window

    mos_mos:          float   # synthetic quality score 1.0–5.0
    mos_label:        str     # "excellent" | "good" | "fair" | "poor" | "bad"
    mos_color:        str     # "green" | "yellow" | "orange" | "red"

    def to_dict(self) -> dict:
        total = self.requests_ok + self.requests_err
        loss_pct = (self.requests_err / total * 100.0) if total > 0 else 0.0
        return {
            # Core controller fields
            "timestamp":        self.timestamp,
            "stream_type":      "web",          # will be overridden by agent wrapper
            "session_id":       self.worker_id, # will be overridden by agent wrapper

            # UDP-style fields (zero/analogous values for web traffic)
            "packets_recv":     self.requests_ok,
            "packets_expected": total,
            "loss_pct":         round(loss_pct, 2),
            "latency_ms":       round(self.page_load_ms, 1),
            "jitter_ms":        0.0,
            "mos_r_factor":     0.0,

            # MOS quality
            "mos_mos":          self.mos_mos,
            "mos_label":        self.mos_label,
            "mos_color":        self.mos_color,

            # Web-specific
            "page_load_ms":     round(self.page_load_ms, 1),
            "bytes_downloaded": self.bytes_downloaded,

            # Metadata
            "worker_id":        self.worker_id,
            "local_ip":         self.local_ip,
            "requests_ok":      self.requests_ok,
            "requests_err":     self.requests_err,
        }


# ---------------------------------------------------------------------------
# Default HTTP headers (realistic browser UA)
# ---------------------------------------------------------------------------

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection":      "keep-alive",
}


# ---------------------------------------------------------------------------
# WebWorker
# ---------------------------------------------------------------------------

class WebWorker:
    """
    Simulates one web-browsing user bound to a specific source IP alias.

    Parameters
    ----------
    worker_id       Unique string, e.g. "web-plan01-u3".  Used as session_id
                    in telemetry so each user appears as a separate row on the
                    dashboard.
    local_ip        Source IP alias to bind to (must already be provisioned by
                    IPAliasManager before calling run()).
    urls            Non-empty list of URLs to cycle through.  HTTP and HTTPS
                    are both supported; SSL certificate verification is skipped
                    for lab environments.
    duration_s      Run duration in seconds.  None = run until stop() is called.
    on_snapshot     Callback invoked with a WebSnapshot every report_interval_s.
                    May be a plain function or an async coroutine.
    report_interval Seconds between telemetry emissions (default 2.0).
    timeout_s       Per-request hard timeout in seconds (default 10.0).
    """

    def __init__(
        self,
        worker_id:       str,
        local_ip:        str,
        urls:            Sequence[str],
        duration_s:      float | None = None,
        on_snapshot:     Callable[[WebSnapshot], None] | None = None,
        report_interval: float = REPORT_INTERVAL_S,
        timeout_s:       float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not urls:
            raise ValueError("WebWorker requires at least one URL")

        self.worker_id       = worker_id
        self.local_ip        = local_ip
        self.urls            = list(urls)
        self.duration_s      = duration_s
        self.on_snapshot     = on_snapshot
        self.report_interval = report_interval
        self.timeout_s       = timeout_s

        self._stop_event = asyncio.Event()
        self._url_index  = 0

        # Per-window accumulators — reset each time a snapshot is emitted
        self._win_load_times: list[float] = []
        self._win_bytes:      int         = 0
        self._win_ok:         int         = 0
        self._win_err:        int         = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to stop after the current fetch completes."""
        self._stop_event.set()

    async def run(self) -> None:
        """
        Main entry point.  Imports aiohttp, creates a source-IP-bound
        connector, then cycles through URLs until stopped or duration elapsed.
        """
        try:
            import aiohttp
        except ImportError:
            logger.error(
                "WebWorker %s: aiohttp is not installed. "
                "Install with: pip install aiohttp --break-system-packages",
                self.worker_id,
            )
            return

        import ssl as _ssl
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = _ssl.CERT_NONE   # lab-safe: skip cert validation

        connector = aiohttp.TCPConnector(
            local_addr          = (self.local_ip, 0),  # bind source IP
            ssl                 = ssl_ctx,
            limit_per_host      = 2,
            enable_cleanup_closed = True,
        )

        logger.info(
            "WebWorker %s starting — ip=%s  urls=%d  duration=%s",
            self.worker_id, self.local_ip, len(self.urls),
            f"{self.duration_s}s" if self.duration_s else "∞",
        )

        try:
            async with aiohttp.ClientSession(
                connector      = connector,
                headers        = _HEADERS,
                connector_owner = True,
            ) as session:
                await self._browse_loop(session)
        except asyncio.CancelledError:
            logger.debug("WebWorker %s: task cancelled", self.worker_id)
            raise
        except Exception as exc:
            logger.error("WebWorker %s: unexpected error: %s", self.worker_id, exc)
        finally:
            # Flush any remaining accumulated data
            if self._win_ok > 0 or self._win_err > 0:
                self._emit_snapshot()
            logger.info("WebWorker %s stopped.", self.worker_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_url(self) -> str:
        url = self.urls[self._url_index % len(self.urls)]
        self._url_index += 1
        return url

    async def _fetch_page(self, session, url: str) -> None:
        """Fetch one URL and accumulate metrics in the current window."""
        import aiohttp
        t0 = time.perf_counter()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                body = await resp.read()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._win_load_times.append(elapsed_ms)
            self._win_bytes += len(body)
            self._win_ok    += 1
            logger.debug(
                "WebWorker %s: GET %s → %d  %.0f ms  %d B",
                self.worker_id, url, resp.status, elapsed_ms, len(body),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._win_err += 1
            logger.debug("WebWorker %s: GET %s failed: %s", self.worker_id, url, exc)

    def _emit_snapshot(self) -> None:
        """Compute window metrics, build a WebSnapshot, and invoke on_snapshot."""
        ok    = self._win_ok
        err   = self._win_err
        total = ok + err

        avg_load = (
            sum(self._win_load_times) / len(self._win_load_times)
            if self._win_load_times else 0.0
        )
        error_rate          = (err / total) if total > 0 else 0.0
        mos, label, color   = _page_load_mos(avg_load, error_rate)

        snapshot = WebSnapshot(
            worker_id        = self.worker_id,
            local_ip         = self.local_ip,
            timestamp        = time.time(),
            requests_ok      = ok,
            requests_err     = err,
            page_load_ms     = round(avg_load, 1),
            bytes_downloaded = self._win_bytes,
            mos_mos          = mos,
            mos_label        = label,
            mos_color        = color,
        )

        # Reset window accumulators
        self._win_load_times.clear()
        self._win_bytes = 0
        self._win_ok    = 0
        self._win_err   = 0

        if self.on_snapshot:
            try:
                if asyncio.iscoroutinefunction(self.on_snapshot):
                    asyncio.ensure_future(self.on_snapshot(snapshot))
                else:
                    self.on_snapshot(snapshot)
            except Exception as exc:
                logger.error(
                    "WebWorker %s: on_snapshot callback error: %s",
                    self.worker_id, exc,
                )

    async def _browse_loop(self, session) -> None:
        """
        Core browse loop: fetch pages in a cycle, emitting telemetry every
        report_interval_s seconds.  Exits cleanly on stop() or duration expiry.
        """
        deadline        = time.time() + self.duration_s if self.duration_s else None
        report_deadline = time.time() + self.report_interval

        while not self._stop_event.is_set():

            # Check duration expiry
            if deadline and time.time() >= deadline:
                logger.debug("WebWorker %s: duration elapsed", self.worker_id)
                break

            # Fetch the next URL in rotation
            url = self._next_url()
            try:
                await asyncio.wait_for(
                    self._fetch_page(session, url),
                    timeout=self.timeout_s + 0.5,
                )
            except asyncio.TimeoutError:
                # wait_for timeout (belt-and-suspenders on top of aiohttp timeout)
                self._win_err += 1
                logger.debug("WebWorker %s: hard timeout on %s", self.worker_id, url)
            except asyncio.CancelledError:
                raise

            # Emit snapshot if the reporting interval has elapsed
            now = time.time()
            if now >= report_deadline:
                self._emit_snapshot()
                report_deadline = now + self.report_interval

            # Brief pause between requests; also gives stop_event a chance to fire
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=MIN_PAUSE_S,
                )
                # stop_event fired during pause → exit cleanly
                break
            except asyncio.TimeoutError:
                pass    # normal case: continue to next page
