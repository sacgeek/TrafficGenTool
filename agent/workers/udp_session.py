"""
UDP session manager.

A "session" is one simulated call leg between two nodes.  The role assigned
to each side controls the port-binding pattern so that stateful
firewalls/routers see a single bidirectional flow rather than two independent
unidirectional ones.

Role model (INITIATOR / RESPONDER)
-----------------------------------
Real Teams media goes through a TURN relay whose well-known port (3478/3479/
3480) appears as BOTH the destination for the client's outbound packets AND
the source for the server's inbound packets.  Stateful packet inspectors
match the two directions as one conversation only when the 5-tuple of the
return path is the exact mirror of the outbound path.

  INITIATOR  – binds an ephemeral source port (Teams src-port range).
               Sends to peer_ip:profile.port.
               Receives return traffic on the SAME socket (the RESPONDER
               sends back from profile.port to this ephemeral port).

  RESPONDER  – binds local_ip:profile.port (the well-known port).
               Receives packets from the INITIATOR; learns the initiator's
               ephemeral source port from the first arriving packet.
               Sends return traffic FROM profile.port BACK TO that
               ephemeral port.

Firewall view of a voice call (e.g. voice profile, port 3478):
  10.1.3.101:50015  ↔  10.1.6.101:3478   ← one bidirectional session ✓

Rate ratios
-----------
Voice / video:  symmetric (responder_rate_ratio = 1.0)
Screenshare:    10:1  (responder_rate_ratio = 0.1 — viewers send only
                RTCP-level control/acknowledgement traffic back)

Legacy roles
------------
SENDER / RECEIVER / BOTH are kept for backward compatibility.  They use
independent sockets on each side and will produce two unidirectional flows
as before.

Usage (from the agent):

    session = UDPSession(
        session_id   = "voice-user3-to-user7",
        stream_type  = StreamType.VOICE,
        role         = SessionRole.INITIATOR,
        local_ip     = "192.168.1.103",
        peer_ip      = "192.168.1.107",
        duration_s   = 60,
        on_snapshot  = agent.publish_snapshot,
    )
    await session.run()
"""

from __future__ import annotations
import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from agent.mos import StreamType, MOSResult, aggregate_mos, calculate_mos
from agent.stream_profiles import PROFILES
from agent.workers.udp_sender import (
    UDPSender, SenderStats,
    HEADER_FORMAT, HEADER_MAGIC, HEADER_SIZE,
)
from agent.workers.udp_receiver import UDPReceiver, StreamSnapshot, StreamState

logger = logging.getLogger(__name__)

# How often to emit a telemetry snapshot upstream
REPORT_INTERVAL_S = 2.0


# ---------------------------------------------------------------------------
# Session role
# ---------------------------------------------------------------------------

class SessionRole(str, Enum):
    SENDER    = "sender"     # send only  (legacy / screenshare source)
    RECEIVER  = "receiver"   # recv only  (legacy / screenshare viewer)
    BOTH      = "both"       # legacy independent bidirectional senders
    INITIATOR = "initiator"  # binds ephemeral port; sends + receives return
    RESPONDER = "responder"  # binds well-known port; receives + sends back


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------

@dataclass
class SessionSummary:
    session_id:   str
    stream_type:  StreamType
    role:         SessionRole
    local_ip:     str
    peer_ip:      str
    start_time:   float
    end_time:     float | None
    snapshots:    list[StreamSnapshot]
    sender_stats: SenderStats | None

    @property
    def duration_s(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def aggregate_mos(self) -> MOSResult | None:
        return aggregate_mos([s.mos for s in self.snapshots]) if self.snapshots else None

    def to_dict(self) -> dict:
        agg = self.aggregate_mos
        return {
            "session_id":     self.session_id,
            "stream_type":    self.stream_type.value,
            "role":           self.role.value,
            "local_ip":       self.local_ip,
            "peer_ip":        self.peer_ip,
            "duration_s":     round(self.duration_s, 1),
            "snapshot_count": len(self.snapshots),
            "mos_aggregate":  agg.to_dict() if agg else None,
            "sender_stats":   self.sender_stats.to_dict() if self.sender_stats else None,
        }


# ---------------------------------------------------------------------------
# Full-duplex protocol — single socket that both sends and receives
# ---------------------------------------------------------------------------

class _FullDuplexProtocol(asyncio.DatagramProtocol):
    """
    asyncio protocol for a UDP socket that both sends and receives NETLAB
    packets.

    on_recv(stream_id, seq, send_ts_us)  called for every valid inbound packet.
    on_peer_addr(ip, port)               called once on the first inbound
                                         packet — RESPONDER uses this to
                                         discover where to aim return traffic.
    """

    def __init__(
        self,
        on_recv:      Callable[[int, int, int], None],
        on_peer_addr: Callable[[str, int], None] | None = None,
    ) -> None:
        self._on_recv      = on_recv
        self._on_peer_addr = on_peer_addr
        self._notified     = False
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if len(data) < HEADER_SIZE:
            return
        try:
            magic, stream_id, seq, send_ts_us = struct.unpack_from(HEADER_FORMAT, data)
        except struct.error:
            return
        if magic != HEADER_MAGIC:
            return
        # Notify caller of peer address on first packet
        if not self._notified and self._on_peer_addr:
            self._on_peer_addr(addr[0], addr[1])
            self._notified = True
        self._on_recv(stream_id, seq, send_ts_us)

    def error_received(self, exc: Exception) -> None:
        logger.warning("FullDuplexProtocol error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


# ---------------------------------------------------------------------------
# UDPSession
# ---------------------------------------------------------------------------

class UDPSession:
    """
    Manages one UDP stream session between two nodes.

    Parameters
    ----------
    session_id   : unique string identifier (e.g. "voice-u3-u7")
    stream_type  : StreamType
    role         : SessionRole
    local_ip     : source IP for this node (one of the node's IP aliases)
    peer_ip      : remote node IP
    duration_s   : session duration in seconds; None = run until stop() called
    on_snapshot  : optional async or sync callback receiving StreamSnapshot
    stream_id    : numeric stream identifier embedded in packets
                   (default: deterministic hash of session_id)
    """

    def __init__(
        self,
        session_id:  str,
        stream_type: StreamType,
        role:        SessionRole,
        local_ip:    str,
        peer_ip:     str,
        duration_s:  float | None = None,
        on_snapshot: Callable[[StreamSnapshot], None] | None = None,
        stream_id:   int | None = None,
    ) -> None:
        self.session_id  = session_id
        self.stream_type = stream_type
        self.role        = role
        self.local_ip    = local_ip
        self.peer_ip     = peer_ip
        self.duration_s  = duration_s
        self.on_snapshot = on_snapshot
        self.stream_id   = stream_id or (abs(hash(session_id)) % (2**31))

        # Legacy role workers
        self._sender:   UDPSender   | None = None
        self._receiver: UDPReceiver | None = None

        self._stop_event = asyncio.Event()

        self._summary = SessionSummary(
            session_id   = session_id,
            stream_type  = stream_type,
            role         = role,
            local_ip     = local_ip,
            peer_ip      = peer_ip,
            start_time   = time.time(),
            end_time     = None,
            snapshots    = [],
            sender_stats = None,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._sender:
            self._sender.stop()
        if self._receiver:
            self._receiver.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_packet(self, seq: int) -> bytes:
        ts_us  = int(time.time() * 1_000_000)
        header = struct.pack(HEADER_FORMAT, HEADER_MAGIC, self.stream_id, seq, ts_us)
        profile = PROFILES[self.stream_type]
        return header + bytes(max(0, profile.packet_size_bytes - HEADER_SIZE))

    def _collect_snapshot(self, snapshot: StreamSnapshot) -> None:
        self._summary.snapshots.append(snapshot)
        if self.on_snapshot:
            try:
                if asyncio.iscoroutinefunction(self.on_snapshot):
                    asyncio.ensure_future(self.on_snapshot(snapshot))
                else:
                    self.on_snapshot(snapshot)
            except Exception as exc:
                logger.error("Session %s snapshot callback error: %s", self.session_id, exc)

    def _emit_snapshot(self, state: StreamState, now: float) -> None:
        """Compute metrics from state window and fire on_snapshot callback."""
        loss_pct, latency_ms, jitter_ms = state.snapshot()
        mos  = calculate_mos(loss_pct, latency_ms, jitter_ms, state.stream_type)
        snap = StreamSnapshot(
            stream_id         = state.stream_id,
            stream_type       = state.stream_type,
            timestamp         = now,
            packets_recv      = state._total_recv,
            packets_expected  = (
                (state.last_seq - state.first_seq + 1)
                if state.first_seq is not None and state.last_seq is not None
                else 0
            ),
            loss_pct          = loss_pct,
            latency_ms        = latency_ms,
            jitter_ms         = jitter_ms,
            mos               = mos,
        )
        self._collect_snapshot(snap)

    async def _report_loop(self, state: StreamState) -> None:
        """Emit a StreamSnapshot every REPORT_INTERVAL_S until stop_event."""
        while not self._stop_event.is_set():
            await asyncio.sleep(REPORT_INTERVAL_S)
            self._emit_snapshot(state, time.time())

    def _duration_watchdog_task(self) -> asyncio.Task | None:
        """Return a watchdog task that fires stop_event after duration_s, or None."""
        if not self.duration_s:
            return None
        async def _watchdog() -> None:
            await asyncio.sleep(self.duration_s)          # type: ignore[arg-type]
            self._stop_event.set()
        return asyncio.create_task(_watchdog(), name=f"watchdog-{self.session_id}")

    # ------------------------------------------------------------------
    # INITIATOR role
    # ------------------------------------------------------------------

    async def _run_initiator(self) -> None:
        """
        INITIATOR:
          - Bind to local_ip:src_port  (ephemeral port from Teams src range).
          - Connect to peer_ip:profile.port  (RESPONDER's well-known port).
          - Send packets at full profile rate via the connected socket.
          - Receive return traffic on the same socket and compute MOS stats.

        Firewall sees:  local_ip:src_port  ↔  peer_ip:profile.port  (one session).
        """
        loop    = asyncio.get_running_loop()
        profile = PROFILES[self.stream_type]
        src_port = profile.pick_src_port()

        recv_state = StreamState(self.stream_id, self.stream_type)

        def _on_recv(stream_id: int, seq: int, send_ts_us: int) -> None:
            # Filter stray packets on the same port; only record our own stream.
            if stream_id == self.stream_id:
                recv_state.record_packet(seq, send_ts_us)

        protocol = _FullDuplexProtocol(on_recv=_on_recv)

        # Connected UDP socket: sendto(data) goes to remote_addr; only receives
        # from remote_addr.  RESPONDER sends back from profile.port → our src_port.
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr  = (self.local_ip, src_port),
            remote_addr = (self.peer_ip, profile.port),
            family      = 10 if ":" in self.local_ip else 2,
        )

        logger.info(
            "Initiator %s: %s:%d → %s:%d  [%s  %.0f pps  %.0f kbps]",
            self.session_id,
            self.local_ip, src_port, self.peer_ip, profile.port,
            self.stream_type.value, profile.packets_per_second, profile.bitrate_kbps,
        )

        # Mutable stats captured by closure (survive task cancellation)
        pkts_sent = 0
        byts_sent = 0
        start_ts  = time.monotonic()

        async def _send() -> None:
            nonlocal pkts_sent, byts_sent
            seq        = 0
            interval_s = profile.interval_ms / 1000.0
            next_send  = time.monotonic()
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_send:
                    pkt = self._build_packet(seq)
                    protocol.transport.sendto(pkt)   # connected → no addr needed
                    pkts_sent += 1
                    byts_sent += len(pkt)
                    seq        += 1
                    next_send  += interval_s
                else:
                    sleep_s = next_send - now - 0.0005
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)

        tasks: list[asyncio.Task] = [
            asyncio.create_task(_send(),                          name=f"init-send-{self.session_id}"),
            asyncio.create_task(self._report_loop(recv_state),   name=f"init-report-{self.session_id}"),
        ]
        watchdog = self._duration_watchdog_task()
        if watchdog:
            tasks.append(watchdog)

        try:
            await self._stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            transport.close()

            stop_ts = time.monotonic()
            elapsed = stop_ts - start_ts
            self._summary.sender_stats = SenderStats(
                stream_id    = self.stream_id,
                stream_type  = self.stream_type,
                packets_sent = pkts_sent,
                bytes_sent   = byts_sent,
                start_time   = start_ts,
                stop_time    = stop_ts,
            )
            logger.info(
                "Initiator %s done: sent %d pkts  %.0f kbps  %.1fs",
                self.session_id, pkts_sent,
                byts_sent * 8 / max(1.0, elapsed) / 1000,
                elapsed,
            )

    # ------------------------------------------------------------------
    # RESPONDER role
    # ------------------------------------------------------------------

    async def _run_responder(self) -> None:
        """
        RESPONDER:
          - Bind to local_ip:profile.port  (the well-known port).
          - Receive packets from the INITIATOR; compute MOS stats.
          - On the first received packet, learn the INITIATOR's ephemeral
            source port, then start sending return traffic FROM profile.port
            BACK TO that port at profile.responder_rate_ratio × full rate.

        Firewall sees:  peer_ip:ephemeral_port  ↔  local_ip:profile.port
                        (same session as the INITIATOR's outbound flow).

        For screenshare (responder_rate_ratio = 0.1), return traffic
        approximates RTCP/control at ~3 fps vs the presenter's ~30 fps.
        """
        loop    = asyncio.get_running_loop()
        profile = PROFILES[self.stream_type]

        recv_state    = StreamState(self.stream_id, self.stream_type)
        peer_addr_evt = asyncio.Event()
        _peer: list[tuple[str, int]] = []   # filled from first inbound packet

        def _on_peer(ip: str, port: int) -> None:
            _peer.append((ip, port))
            peer_addr_evt.set()
            logger.info(
                "Responder %s: initiator discovered at %s:%d — starting %.1f pps return stream",
                self.session_id, ip, port,
                (1000.0 / profile.interval_ms) * profile.responder_rate_ratio,
            )

        def _on_recv(stream_id: int, seq: int, send_ts_us: int) -> None:
            # Filter stray packets on the same port; only record our own stream.
            if stream_id == self.stream_id:
                recv_state.record_packet(seq, send_ts_us)

        protocol = _FullDuplexProtocol(
            on_recv      = _on_recv,
            on_peer_addr = _on_peer,
        )

        # Unconnected socket — can receive from any source and sendto any addr
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr = (self.local_ip, profile.port),
            family     = 10 if ":" in self.local_ip else 2,
        )

        logger.info(
            "Responder %s: listening on %s:%d  [%s]",
            self.session_id, self.local_ip, profile.port, self.stream_type.value,
        )

        pkts_sent = 0
        byts_sent = 0
        start_ts  = time.monotonic()

        return_interval_s = (profile.interval_ms / 1000.0) / profile.responder_rate_ratio

        async def _return_send() -> None:
            nonlocal pkts_sent, byts_sent
            # Block until the INITIATOR's first packet arrives (learn src port)
            try:
                await asyncio.wait_for(peer_addr_evt.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Responder %s: no inbound packets from initiator after 30 s — "
                    "return traffic suppressed",
                    self.session_id,
                )
                return

            target    = _peer[0]
            seq       = 0
            next_send = time.monotonic()
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_send:
                    pkt = self._build_packet(seq)
                    protocol.transport.sendto(pkt, target)
                    pkts_sent += 1
                    byts_sent += len(pkt)
                    seq        += 1
                    next_send  += return_interval_s
                else:
                    sleep_s = next_send - now - 0.0005
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)

        tasks: list[asyncio.Task] = [
            asyncio.create_task(_return_send(),                   name=f"resp-send-{self.session_id}"),
            asyncio.create_task(self._report_loop(recv_state),    name=f"resp-report-{self.session_id}"),
        ]
        watchdog = self._duration_watchdog_task()
        if watchdog:
            tasks.append(watchdog)

        try:
            await self._stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            transport.close()

            stop_ts = time.monotonic()
            elapsed = stop_ts - start_ts
            self._summary.sender_stats = SenderStats(
                stream_id    = self.stream_id,
                stream_type  = self.stream_type,
                packets_sent = pkts_sent,
                bytes_sent   = byts_sent,
                start_time   = start_ts,
                stop_time    = stop_ts,
            )
            logger.info(
                "Responder %s done: sent %d return pkts  %.1fs",
                self.session_id, pkts_sent, elapsed,
            )

    # ------------------------------------------------------------------
    # Public run method
    # ------------------------------------------------------------------

    async def run(self) -> SessionSummary:
        """Start the session and block until it ends."""
        profile = PROFILES[self.stream_type]
        logger.info(
            "Session %s starting: role=%s  type=%s  %s ↔ %s  port=%d",
            self.session_id, self.role.value, self.stream_type.value,
            self.local_ip, self.peer_ip, profile.port,
        )

        if self.role == SessionRole.INITIATOR:
            await self._run_initiator()

        elif self.role == SessionRole.RESPONDER:
            await self._run_responder()

        else:
            # -------------------------------------------------------
            # Legacy roles: SENDER, RECEIVER, BOTH
            # Each side has its own independent socket — produces two
            # separate unidirectional flows as seen by stateful devices.
            # -------------------------------------------------------
            tasks: list[asyncio.Task] = []

            if self.role in (SessionRole.RECEIVER, SessionRole.BOTH):
                self._receiver = UDPReceiver(
                    stream_type = self.stream_type,
                    bind_ip     = self.local_ip,
                    on_snapshot = self._collect_snapshot,
                )
                tasks.append(asyncio.create_task(
                    self._receiver.run(), name=f"recv-{self.session_id}"
                ))

            if self.role in (SessionRole.SENDER, SessionRole.BOTH):
                self._sender = UDPSender(
                    stream_id   = self.stream_id,
                    stream_type = self.stream_type,
                    src_ip      = self.local_ip,
                    dst_ip      = self.peer_ip,
                    duration_s  = self.duration_s,
                )
                tasks.append(asyncio.create_task(
                    self._sender.run(), name=f"send-{self.session_id}"
                ))

            watchdog = self._duration_watchdog_task()
            if watchdog:
                tasks.append(watchdog)

            await self._stop_event.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if self._sender:
                self._summary.sender_stats = self._sender.stats

        self._summary.end_time = time.time()
        logger.info(
            "Session %s ended after %.1fs  snapshots=%d",
            self.session_id, self._summary.duration_s, len(self._summary.snapshots),
        )
        return self._summary

    @property
    def summary(self) -> SessionSummary:
        return self._summary
