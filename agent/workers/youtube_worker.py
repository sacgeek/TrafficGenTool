"""
YouTube worker: simulates streaming video users via yt-dlp.

Each YoutubeWorker represents one user streaming video from a specific source
IP alias.  It launches yt-dlp as a subprocess (with --source-address for real
source-IP binding), downloads to /dev/null (discards content, measures
throughput only), parses the progress output to derive download rate, and emits
quality telemetry every 2 seconds.

When a download ends the worker restarts immediately with the same URL,
maintaining continuous network load for the full test duration.

Source IP binding — yt-dlp passes the IP to the OS via bind() before opening
the TCP connection to the CDN, so the traffic is genuinely visible on the
wire as originating from that alias.

Telemetry emitted every REPORT_INTERVAL_S:
    throughput_kbps   mean download rate in the window (kbps)
    bytes_downloaded  cumulative bytes received from the CDN in the window
    stall_seconds     total seconds where rate fell below STALL_THRESHOLD_RATIO
                      × the quality-tier minimum bitrate
    mos_mos           quality score 1.0–5.0 (throughput vs required + stall penalty)

Usage (from PlanExecutor in agent.py):

    worker = YoutubeWorker(
        worker_id  = "yt-plan01-u2",
        local_ip   = "192.168.1.102",
        url        = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        quality    = "720p",
        duration_s = 60,
        on_snapshot = agent.on_snapshot_callback,
    )
    task = asyncio.create_task(worker.run())
    # …later…
    worker.stop()
    await task

Place this file at:  agent/workers/youtube_worker.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_INTERVAL_S    = 2.0    # telemetry cadence (matches UDP and web workers)
STALL_THRESHOLD_RATIO = 0.8   # rate < this × min_kbps → counted as a stall

# Default URL: a 11-hour recorded (non-live) ambient video used as a stable,
# long-lived bandwidth sink.  With --limit-rate pacing the download to the
# quality tier's required bitrate, a single yt-dlp process will run for the
# entire test duration without restarting.
# IMPORTANT: must be a regular uploaded video, NOT a live stream — yt-dlp
# requires different flags for live streams and will exit code 1 otherwise.
# For isolated lab networks, replace with an internal streaming server URL.
DEFAULT_URL = "https://www.youtube.com/watch?v=UgHKb_7884o"

# ---------------------------------------------------------------------------
# Quality profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityProfile:
    label:     str    # "360p", "480p", "720p", "1080p"
    yt_format: str    # yt-dlp --format string
    min_kbps:  float  # minimum download rate (kbps) to sustain this quality

# Format strings use fallback chaining: prefer a combined stream, fall back to
# the best available video+audio merge so the worker still runs on all URLs.
QUALITY_PROFILES: dict[str, QualityProfile] = {
    "360p":  QualityProfile(
        "360p",
        "best[height<=360]/bestvideo[height<=360]+bestaudio/best",
        500,
    ),
    "480p":  QualityProfile(
        "480p",
        "best[height<=480]/bestvideo[height<=480]+bestaudio/best",
        1_500,
    ),
    "720p":  QualityProfile(
        "720p",
        "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
        3_000,
    ),
    "1080p": QualityProfile(
        "1080p",
        "best[height<=1080]/bestvideo[height<=1080]+bestaudio/best",
        8_000,
    ),
}

DEFAULT_QUALITY = "720p"

# ---------------------------------------------------------------------------
# yt-dlp progress output parsing
# ---------------------------------------------------------------------------
#
# yt-dlp (with --newline) emits one line per progress update, e.g.:
#   [download]  14.2% of ~ 89.47MiB at  2.34MiB/s ETA 00:42
#   [download]   0.0% of ~  1.00GiB at 512.00KiB/s ETA 02:47 (frag 1/3)
#   [download] 100% of ~ 89.47MiB at   5.12MiB/s ETA 00:00

_PROGRESS_RE = re.compile(
    r"\[download\].*?"          # starts with [download]
    r"at\s+"                    # followed (anywhere) by "at "
    r"(?P<rate>[\d.]+)\s*"      # the numeric rate value
    r"(?P<unit>[KMGkmg]i?[Bb]/s)"  # the unit: KiB/s, MiB/s, etc.
)

# Bytes-per-second multipliers keyed by lowercased unit string
_UNIT_BPS: dict[str, float] = {
    "b/s":   1.0,
    "kb/s":  1_000.0,
    "mb/s":  1_000_000.0,
    "gb/s":  1_000_000_000.0,
    "kib/s": 1_024.0,
    "mib/s": 1_048_576.0,
    "gib/s": 1_073_741_824.0,
}


def parse_rate_bps(rate_str: str, unit_str: str) -> float:
    """
    Convert a yt-dlp rate string and unit to bytes per second.
    Returns 0.0 on any parse failure.
    """
    try:
        return float(rate_str) * _UNIT_BPS.get(unit_str.lower(), 0.0)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# MOS scoring for streaming video
# ---------------------------------------------------------------------------
#
# Maps download throughput ratio (actual_kbps / required_kbps) to a raw MOS.
# Breakpoints chosen so the labels align with the dashboard colour thresholds.

_RATIO_BREAKPOINTS: list[tuple[float, float]] = [
    (0.0,  1.0),   # no throughput  → bad
    (0.5,  2.0),   # 50% required   → poor
    (0.8,  3.1),   # 80% required   → borderline poor
    (1.0,  3.6),   # 100% required  → fair
    (1.5,  4.0),   # 150% required  → good
    (2.0,  4.3),   # 200% required  → excellent
    (3.0,  5.0),   # 300% required  → perfect
]


def _ratio_to_raw_mos(ratio: float) -> float:
    """Piecewise-linear mapping from throughput ratio to a raw MOS value."""
    if ratio <= 0:
        return 1.0
    for i in range(len(_RATIO_BREAKPOINTS) - 1):
        x0, y0 = _RATIO_BREAKPOINTS[i]
        x1, y1 = _RATIO_BREAKPOINTS[i + 1]
        if ratio <= x1:
            span = x1 - x0
            t = (ratio - x0) / span if span > 0 else 0.0
            return y0 + t * (y1 - y0)
    return 5.0


def youtube_mos(
    avg_kbps:       float,
    required_kbps:  float,
    stall_fraction: float,   # 0.0–1.0 fraction of the window that was stalled
) -> tuple[float, str, str]:
    """
    Derive a 1.0–5.0 MOS-style score from YouTube streaming metrics.

    Stall penalty: a fully stalled window reduces the score by up to 1.5 MOS
    points, reflecting the subjective badness of a freezing stream vs. merely
    a slow one.

    Returns (mos, label, color).
    """
    ratio = avg_kbps / max(1.0, required_kbps)
    raw   = _ratio_to_raw_mos(ratio)
    mos   = max(1.0, min(5.0, round(raw - stall_fraction * 1.5, 2)))

    if mos >= 4.3:
        label, color = "excellent", "green"
    elif mos >= 4.0:
        label, color = "good",      "green"
    elif mos >= 3.6:
        label, color = "fair",      "yellow"
    elif mos >= 3.1:
        label, color = "poor",      "orange"
    else:
        label, color = "bad",       "red"

    return mos, label, color


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------

@dataclass
class YoutubeSnapshot:
    """
    Telemetry report for one YouTube worker window, emitted every REPORT_INTERVAL_S.
    to_dict() is wire-compatible with the controller's StreamSnapshot Pydantic model.
    """
    worker_id:        str
    local_ip:         str
    timestamp:        float
    quality:          str    # "360p" | "480p" | "720p" | "1080p"

    throughput_kbps:  float  # mean measured download rate in the window (kbps)
    bytes_downloaded: int    # total bytes received from CDN in the window
    stall_seconds:    float  # seconds where rate fell below stall threshold

    mos_mos:          float
    mos_label:        str
    mos_color:        str

    def to_dict(self) -> dict:
        # Express stall_seconds as a loss_pct analogue so the dashboard can
        # colour the row — a fully stalled window = 100% "loss".
        loss_pct = min(100.0, round(
            self.stall_seconds / max(0.001, REPORT_INTERVAL_S) * 100.0, 1
        ))
        # Estimate received packets from bytes downloaded using the standard
        # TCP maximum segment size (1460 bytes payload).  This is an
        # approximation — actual segment sizes vary — but it gives the
        # dashboard a meaningful non-zero counter that tracks real traffic
        # rather than a hardcoded 0.
        _TCP_MSS = 1460
        est_packets = self.bytes_downloaded // _TCP_MSS

        return {
            # Required controller StreamSnapshot fields
            "timestamp":        self.timestamp,
            "stream_type":      "youtube",        # overridden by agent wrapper
            "session_id":       self.worker_id,   # overridden by agent wrapper
            "packets_recv":     est_packets,
            "packets_expected": est_packets,      # no loss expected for TCP
            "loss_pct":         loss_pct,
            "latency_ms":       0.0,
            "jitter_ms":        0.0,
            "mos_r_factor":     0.0,
            "mos_mos":          self.mos_mos,
            "mos_label":        self.mos_label,
            "mos_color":        self.mos_color,
            # Web/YouTube-specific fields (understood by the controller model)
            "page_load_ms":     None,
            "bytes_downloaded": self.bytes_downloaded,
            # YouTube-specific extras (passed through for dashboard display)
            "worker_id":        self.worker_id,
            "local_ip":         self.local_ip,
            "quality":          self.quality,
            "throughput_kbps":  round(self.throughput_kbps, 1),
            "stall_seconds":    round(self.stall_seconds, 2),
        }


# ---------------------------------------------------------------------------
# YoutubeWorker
# ---------------------------------------------------------------------------

class YoutubeWorker:
    """
    Simulates one YouTube streaming user bound to a specific source IP alias.

    Parameters
    ----------
    worker_id       Unique string, e.g. "yt-plan01-u2".  Used as session_id in
                    telemetry so each user appears as a separate dashboard row.
    local_ip        Source IP alias to bind to (must be provisioned before run()).
    url             Video URL.  Any yt-dlp-supported URL works, including internal
                    streaming servers (e.g. an Nginx HLS/DASH endpoint).
    quality         "360p" | "480p" | "720p" | "1080p".  Default: "720p".
    duration_s      Run duration in seconds.  None = run until stop() is called.
    on_snapshot     Callback(YoutubeSnapshot) invoked every report_interval_s.
                    May be a plain function or an async coroutine.
    report_interval Seconds between telemetry emissions (default: 2.0).
    """

    def __init__(
        self,
        worker_id:       str,
        local_ip:        str,
        url:             str,
        quality:         str           = DEFAULT_QUALITY,
        duration_s:      float | None  = None,
        on_snapshot:     Callable[[YoutubeSnapshot], None] | None = None,
        report_interval: float         = REPORT_INTERVAL_S,
    ) -> None:
        if quality not in QUALITY_PROFILES:
            raise ValueError(
                f"Unknown quality {quality!r}. "
                f"Valid choices: {sorted(QUALITY_PROFILES)}"
            )
        self.worker_id       = worker_id
        self.local_ip        = local_ip
        self.url             = url
        self.quality         = quality
        self.profile         = QUALITY_PROFILES[quality]
        self.duration_s      = duration_s
        self.on_snapshot     = on_snapshot
        self.report_interval = report_interval

        self._stop_event   = asyncio.Event()
        self._current_proc = None

        # Per-window accumulators (reset each time a snapshot is emitted)
        self._win_rates_bps: list[float] = []   # one entry per parsed progress line
        self._win_bytes:     int         = 0
        self._win_stall_s:   float       = 0.0
        self._last_rate_ts:  float       = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the worker to stop.  Kills the active yt-dlp process immediately."""
        self._stop_event.set()
        if self._current_proc and self._current_proc.returncode is None:
            try:
                self._current_proc.kill()
            except ProcessLookupError:
                pass

    async def run(self) -> None:
        """
        Main entry point.  Launches yt-dlp in a loop, restarting each time
        a download completes, until stopped or duration elapses.
        """
        ytdlp = _find_ytdlp()
        if not ytdlp:
            logger.error(
                "YoutubeWorker %s: yt-dlp not found. "
                "Install with: pip install yt-dlp --break-system-packages",
                self.worker_id,
            )
            return

        deadline = time.time() + self.duration_s if self.duration_s else None

        logger.info(
            "YoutubeWorker %s starting — ip=%s  quality=%s  url=%s  duration=%s",
            self.worker_id, self.local_ip, self.quality, self.url,
            f"{self.duration_s}s" if self.duration_s else "∞",
        )

        # Snapshot emitter runs in parallel with the download loop
        emitter = asyncio.create_task(
            self._snapshot_loop(), name=f"yt-snap-{self.worker_id}"
        )

        try:
            while not self._stop_event.is_set():
                if deadline and time.time() >= deadline:
                    break
                remaining = (deadline - time.time()) if deadline else None
                await self._run_once(ytdlp, remaining)
                if not self._stop_event.is_set():
                    # Brief pause between restarts; also lets stop_event fire
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._stop_event.wait()),
                            timeout=1.0,
                        )
                        break
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("YoutubeWorker %s: unexpected error: %s", self.worker_id, exc)
        finally:
            emitter.cancel()
            try:
                await emitter
            except asyncio.CancelledError:
                pass
            # Flush any remaining window data
            if self._win_rates_bps or self._win_bytes > 0:
                self._emit_snapshot()
            logger.info("YoutubeWorker %s stopped.", self.worker_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_once(self, ytdlp: str, max_seconds: float | None) -> None:
        """
        Launch one yt-dlp process, read its stderr until the download ends
        or a stop/timeout is triggered.
        """
        # Throttle to 110 % of the quality tier's required bitrate so the
        # download paces like a real viewer session.  Without a rate limit
        # yt-dlp saturates the link, finishes the video in seconds, and the
        # constant process restarts appear as many short-lived flows on the
        # router rather than one persistent stream.
        # min_kbps is in kilo-bits/s; yt-dlp --limit-rate takes bytes/s (K suffix).
        limit_kbps   = self.profile.min_kbps * 1.1          # 10 % headroom
        limit_kBps   = max(1, round(limit_kbps / 8))        # convert to KB/s
        limit_rate   = f"{limit_kBps}K"

        cmd = [
            ytdlp,
            "--source-address", self.local_ip,
            "--format",         self.profile.yt_format,
            "--newline",        # one progress line per update (easy to parse)
            "--no-playlist",
            "--no-warnings",    # suppress warnings but keep progress output
            "--progress",       # show progress bar (do NOT use --quiet: it silences
                                # _screen_file even when stderr is a pipe, causing yt-dlp
                                # to produce no output regardless of --progress)
            "--no-part",        # no .part temp files
            "--limit-rate",     limit_rate,   # pace download to real viewing speed
            "--retries",        "1",          # fail fast — our restart loop handles retries
            "--fragment-retries", "1",        # same for individual DASH/HLS fragments
            "-o", "/dev/null",  # discard content — we only measure throughput
            self.url,
        ]
        logger.debug("YoutubeWorker %s: %s", self.worker_id, " ".join(cmd))

        # PYTHONUNBUFFERED forces line-by-line flushing when stderr is a pipe.
        # Without it, Python block-buffers writes and progress lines only arrive
        # in our reader when the buffer fills (64 KB) or the process exits.
        _env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                # yt-dlp routes progress through _out_files['screen'] which is
                # sys.stdout when an output file (not '-') is specified.  Merge
                # stderr into stdout so we capture everything from proc.stdout
                # regardless of which fd yt-dlp chooses.
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_env,
            )
        except FileNotFoundError:
            logger.error(
                "YoutubeWorker %s: yt-dlp binary not found at %s", self.worker_id, ytdlp
            )
            return
        except Exception as exc:
            logger.error("YoutubeWorker %s: failed to start yt-dlp: %s", self.worker_id, exc)
            return

        self._current_proc = proc
        self._last_rate_ts = time.time()

        read_task = asyncio.create_task(
            self._read_output(proc, proc.stdout), name=f"yt-read-{self.worker_id}"
        )
        stop_task = asyncio.create_task(
            self._stop_event.wait(), name=f"yt-stopwait-{self.worker_id}"
        )

        # Guard against a download that runs past the plan duration
        timeout = (max_seconds + 2.0) if max_seconds else None
        try:
            done, pending = await asyncio.wait(
                {read_task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            # Ensure the process is dead (normal / stop-event / timeout path)
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            # _teardown() cancelled the worker task while we were waiting.
            # The code after asyncio.wait() would never run, so yt-dlp would
            # be left alive as an orphan — kill it here before propagating.
            for t in (read_task, stop_task):
                if not t.done():
                    t.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise  # re-raise so the task is properly marked cancelled

        self._current_proc = None
        rc = proc.returncode
        if rc not in (None, 0, -9):  # -9 = SIGKILL, expected on stop()
            logger.warning(
                "YoutubeWorker %s: yt-dlp exited with code %s (url=%s)",
                self.worker_id, rc, self.url,
            )

    async def _read_output(self, proc, stream) -> None:
        """Read yt-dlp output line-by-line, parsing progress updates.

        `stream` is proc.stdout (stderr is merged into it via STDOUT redirect).
        yt-dlp routes progress through _out_files['screen'] = sys.stdout when
        an output file is specified, so capturing stdout is required.

        Progress lines are parsed for throughput metrics.  All other lines
        (errors, warnings, info) are logged at DEBUG level immediately so they
        appear in real-time when running with --log-level DEBUG, and are also
        collected for a WARNING-level summary if the process exits non-zero.
        """
        error_lines: list[str] = []
        async for raw_line in stream:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if _PROGRESS_RE.search(line):
                self._parse_progress_line(line)
            else:
                logger.debug("YoutubeWorker %s [yt-dlp]: %s", self.worker_id, line)
                error_lines.append(line)

        await proc.wait()

        if proc.returncode not in (None, 0, -9) and error_lines:
            logger.warning(
                "YoutubeWorker %s: yt-dlp stderr (rc=%s):\n  %s",
                self.worker_id, proc.returncode,
                "\n  ".join(error_lines[-20:]),   # last 20 lines to keep logs manageable
            )

    def _parse_progress_line(self, line: str) -> None:
        """
        Extract download rate from one yt-dlp progress line and update
        the current window's accumulators.
        """
        m = _PROGRESS_RE.search(line)
        if not m:
            return

        rate_bps = parse_rate_bps(m.group("rate"), m.group("unit"))
        logger.debug(
            "YoutubeWorker %s: parsed rate=%.0f bps (%.1f kbps) from: %s",
            self.worker_id, rate_bps, rate_bps * 8 / 1000, line,
        )
        now      = time.time()

        # Accumulate bytes in this window using the trapezoidal approximation:
        # bytes ≈ rate × time_since_last_line
        if rate_bps > 0 and self._last_rate_ts > 0:
            dt = now - self._last_rate_ts
            if 0 < dt < 5.0:    # ignore gaps > 5 s (e.g., format-resolution delay)
                self._win_bytes += int(rate_bps * dt)

                # Stall: rate fell below threshold × required bitrate
                threshold_bps = (
                    self.profile.min_kbps * 1_000 / 8 * STALL_THRESHOLD_RATIO
                )
                if rate_bps < threshold_bps:
                    self._win_stall_s += dt

        if rate_bps > 0:
            self._win_rates_bps.append(rate_bps)

        self._last_rate_ts = now

    def _emit_snapshot(self) -> None:
        """Compute window statistics, build a YoutubeSnapshot, call on_snapshot.

        If no progress lines arrived in this window (e.g. during the brief gap
        between a completed download and the next yt-dlp restart, or while
        yt-dlp is resolving the format before the first byte arrives) there is
        no meaningful throughput to report.  Emitting a 0-kbps snapshot in
        that case would produce MOS 1.0 and pollute the rolling average, so we
        skip it entirely and let the accumulators roll over into the next window.
        """
        if not self._win_rates_bps:
            # Nothing to report yet — window accumulators stay untouched so
            # any partial stall time carries forward into the next window.
            return

        avg_kbps = (sum(self._win_rates_bps) / len(self._win_rates_bps)) * 8 / 1_000.0

        stall_frac = min(1.0, self._win_stall_s / max(0.001, self.report_interval))
        mos, label, color = youtube_mos(avg_kbps, self.profile.min_kbps, stall_frac)

        snapshot = YoutubeSnapshot(
            worker_id        = self.worker_id,
            local_ip         = self.local_ip,
            timestamp        = time.time(),
            quality          = self.quality,
            throughput_kbps  = round(avg_kbps, 1),
            bytes_downloaded = self._win_bytes,
            stall_seconds    = round(self._win_stall_s, 2),
            mos_mos          = mos,
            mos_label        = label,
            mos_color        = color,
        )

        # Reset window accumulators
        self._win_rates_bps.clear()
        self._win_bytes   = 0
        self._win_stall_s = 0.0

        if self.on_snapshot:
            try:
                if asyncio.iscoroutinefunction(self.on_snapshot):
                    asyncio.ensure_future(self.on_snapshot(snapshot))
                else:
                    self.on_snapshot(snapshot)
            except Exception as exc:
                logger.error(
                    "YoutubeWorker %s: on_snapshot error: %s", self.worker_id, exc
                )

    async def _snapshot_loop(self) -> None:
        """Emit telemetry at regular intervals until stopped."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self.report_interval,
                )
                break   # stop event fired
            except asyncio.TimeoutError:
                self._emit_snapshot()


# ---------------------------------------------------------------------------
# yt-dlp binary discovery
# ---------------------------------------------------------------------------

def _find_ytdlp() -> str | None:
    """Return the path to a yt-dlp compatible binary, or None if not found."""
    for name in ("yt-dlp", "yt_dlp", "youtube-dl"):
        path = shutil.which(name)
        if path:
            return path
    return None
