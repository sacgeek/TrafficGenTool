"""
UDP stream receiver worker.

Listens on a port, validates the packet header, and accumulates per-stream
statistics.  Every `report_interval_s` seconds it computes:

  packet loss %    = (expected - received) / expected × 100
  one-way latency  = receive_time - send_timestamp  (requires NTP sync
                     between nodes; still useful for relative comparisons
                     even without perfect clock sync)
  jitter           = RFC 3550 running jitter estimate

These are fed into the MOS engine and the result is emitted as a
StreamSnapshot that the agent ships back to the controller.

Supports multiple concurrent streams on different ports by running
one UDPReceiver instance per profile port.
"""

from __future__ import annotations
import asyncio
import logging
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from agent.workers.udp_sender import HEADER_FORMAT, HEADER_MAGIC, HEADER_SIZE
from agent.mos import StreamType, calculate_mos, MOSResult
from agent.stream_profiles import PROFILES

logger = logging.getLogger(__name__)

# How often (seconds) we emit a stats snapshot upstream
REPORT_INTERVAL_S = 2.0

# Sliding window size for jitter/latency rolling averages
WINDOW_SIZE = 100


@dataclass
class StreamSnapshot:
    """One telemetry report for a single stream, emitted every REPORT_INTERVAL_S."""
    stream_id:      int
    stream_type:    StreamType
    timestamp:      float           # Unix time of this snapshot
    packets_recv:   int
    packets_expected: int
    loss_pct:       float
    latency_ms:     float           # mean one-way latency in window
    jitter_ms:      float           # RFC-3550 jitter estimate
    mos:            MOSResult

    def to_dict(self) -> dict:
        return {
            "stream_id":        self.stream_id,
            "stream_type":      self.stream_type.value,
            "timestamp":        self.timestamp,
            "packets_recv":     self.packets_recv,
            "packets_expected": self.packets_expected,
            "loss_pct":         round(self.loss_pct, 2),
            "latency_ms":       round(self.latency_ms, 2),
            "jitter_ms":        round(self.jitter_ms, 2),
            **{f"mos_{k}": v for k, v in self.mos.to_dict().items()},
        }


class StreamState:
    """Tracks running stats for a single stream_id."""

    def __init__(self, stream_id: int, stream_type: StreamType) -> None:
        self.stream_id   = stream_id
        self.stream_type = stream_type

        # Sequence tracking
        self.first_seq: int | None = None
        self.last_seq:  int | None = None
        self.recv_count_window: int = 0

        # Latency (one-way, microseconds)
        self._latency_window: deque[float] = deque(maxlen=WINDOW_SIZE)

        # RFC-3550 jitter (transit time variance, microseconds)
        self._last_transit: float | None = None
        self._jitter_us: float = 0.0

        # Window start for loss calculation
        self._window_start_seq: int | None = None
        self._window_start_count: int = 0
        self._total_recv: int = 0

    def record_packet(self, seq: int, send_ts_us: int) -> None:
        now_us = time.time() * 1_000_000

        # Sequence bookkeeping
        if self.first_seq is None:
            self.first_seq = seq
            self._window_start_seq = seq
        self.last_seq = max(self.last_seq or 0, seq)
        self._total_recv  += 1
        self.recv_count_window += 1

        # One-way latency (meaningful only with clock sync)
        transit_us = now_us - send_ts_us
        # Cap to avoid wild spikes from clock skew at startup
        if abs(transit_us) < 5_000_000:   # ignore if > 5 s (clock unsync'd)
            self._latency_window.append(transit_us)

            # RFC-3550 jitter
            if self._last_transit is not None:
                d = abs(transit_us - self._last_transit)
                self._jitter_us += (d - self._jitter_us) / 16.0
            self._last_transit = transit_us

    def snapshot(self) -> tuple[float, float, float]:
        """
        Returns (loss_pct, latency_ms, jitter_ms) for the current window
        and resets the window counters.
        """
        # Loss
        if self._window_start_seq is not None and self.last_seq is not None:
            expected = (self.last_seq - self._window_start_seq + 1)
            received = self.recv_count_window
            loss_pct = max(0.0, (expected - received) / max(1, expected) * 100.0)
        else:
            loss_pct = 0.0

        # Latency
        if self._latency_window:
            latency_ms = (sum(self._latency_window) / len(self._latency_window)) / 1000.0
        else:
            latency_ms = 0.0

        # Jitter
        jitter_ms = self._jitter_us / 1000.0

        # Reset window
        self._window_start_seq   = (self.last_seq or 0) + 1
        self.recv_count_window   = 0

        return loss_pct, latency_ms, jitter_ms


class UDPReceiverProtocol(asyncio.DatagramProtocol):
    """asyncio protocol that parses incoming packets."""

    def __init__(self, on_packet: Callable[[int, int, int], None]) -> None:
        self._on_packet = on_packet  # (stream_id, seq, send_ts_us)

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if len(data) < HEADER_SIZE:
            return
        try:
            magic, stream_id, seq, send_ts_us = struct.unpack_from(HEADER_FORMAT, data)
        except struct.error:
            return
        if magic != HEADER_MAGIC:
            return
        self._on_packet(stream_id, seq, send_ts_us)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Receiver protocol error: %s", exc)


class UDPReceiver:
    """
    Listens on bind_ip:port, collects stream statistics, and periodically
    invokes `on_snapshot` with a StreamSnapshot for each active stream.

    Parameters
    ----------
    stream_type     : which stream profile this receiver handles
    bind_ip         : IP to bind (usually '0.0.0.0' or a specific alias)
    port            : UDP port to listen on (defaults to profile port)
    on_snapshot     : async callback(StreamSnapshot)
    report_interval : seconds between telemetry emissions
    """

    def __init__(
        self,
        stream_type:     StreamType,
        bind_ip:         str = "0.0.0.0",
        port:            int | None = None,
        on_snapshot:     Callable[[StreamSnapshot], None] | None = None,
        report_interval: float = REPORT_INTERVAL_S,
    ) -> None:
        self.stream_type     = stream_type
        self.bind_ip         = bind_ip
        self.port            = port or PROFILES[stream_type].port
        self.on_snapshot     = on_snapshot
        self.report_interval = report_interval
        self._streams: dict[int, StreamState] = {}
        self._stop_event     = asyncio.Event()
        self._transport      = None

    def stop(self) -> None:
        self._stop_event.set()

    def _on_packet(self, stream_id: int, seq: int, send_ts_us: int) -> None:
        if stream_id not in self._streams:
            self._streams[stream_id] = StreamState(stream_id, self.stream_type)
            logger.info(
                "Receiver %s: new stream %d detected",
                self.stream_type.value, stream_id,
            )
        self._streams[stream_id].record_packet(seq, send_ts_us)

    async def _report_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.report_interval)
            now = time.time()
            for state in list(self._streams.values()):
                loss_pct, latency_ms, jitter_ms = state.snapshot()
                mos = calculate_mos(loss_pct, latency_ms, jitter_ms, state.stream_type)
                snapshot = StreamSnapshot(
                    stream_id      = state.stream_id,
                    stream_type    = state.stream_type,
                    timestamp      = now,
                    packets_recv   = state._total_recv,
                    packets_expected = (state.last_seq - state.first_seq + 1)
                                       if state.first_seq is not None and state.last_seq is not None
                                       else 0,
                    loss_pct       = loss_pct,
                    latency_ms     = latency_ms,
                    jitter_ms      = jitter_ms,
                    mos            = mos,
                )
                if self.on_snapshot:
                    try:
                        if asyncio.iscoroutinefunction(self.on_snapshot):
                            await self.on_snapshot(snapshot)
                        else:
                            self.on_snapshot(snapshot)
                    except Exception as exc:
                        logger.error("on_snapshot callback failed: %s", exc)

                logger.debug(
                    "Stream %d [%s]: loss=%.1f%%  lat=%.1fms  jitter=%.1fms  MOS=%.2f (%s)",
                    state.stream_id, state.stream_type.value,
                    loss_pct, latency_ms, jitter_ms,
                    mos.mos, mos.label,
                )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: UDPReceiverProtocol(self._on_packet),
            local_addr=(self.bind_ip, self.port),
        )
        logger.info(
            "Receiver listening on %s:%d for %s streams",
            self.bind_ip, self.port, self.stream_type.value,
        )
        try:
            await self._report_loop()
        finally:
            if self._transport:
                self._transport.close()
            logger.info("Receiver %s:%d stopped", self.bind_ip, self.port)
