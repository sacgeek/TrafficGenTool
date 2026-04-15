"""
UDP session manager.

A "session" is one simulated call between two nodes:
  - VOICE / VIDEO  : bidirectional (sender on each side)
  - SCREENSHARE    : one sender → all configured receivers

The manager is responsible for:
  1. Starting/stopping sender and receiver coroutines.
  2. Collecting StreamSnapshots and maintaining a rolling history.
  3. Providing a clean summary dict for the controller to consume.

Usage (from the agent):

    session = UDPSession(
        session_id   = "voice-user3-to-user7",
        stream_type  = StreamType.VOICE,
        role         = SessionRole.SENDER,    # or RECEIVER or BOTH
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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from agent.mos import StreamType, MOSResult, aggregate_mos
from agent.stream_profiles import PROFILES
from agent.workers.udp_sender import UDPSender, SenderStats
from agent.workers.udp_receiver import UDPReceiver, StreamSnapshot

logger = logging.getLogger(__name__)


class SessionRole(str, Enum):
    SENDER   = "sender"     # only sends (screenshare source)
    RECEIVER = "receiver"   # only receives (screenshare viewer)
    BOTH     = "both"       # bidirectional (voice/video)


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
            "session_id":  self.session_id,
            "stream_type": self.stream_type.value,
            "role":        self.role.value,
            "local_ip":    self.local_ip,
            "peer_ip":     self.peer_ip,
            "duration_s":  round(self.duration_s, 1),
            "snapshot_count": len(self.snapshots),
            "mos_aggregate": agg.to_dict() if agg else None,
            "sender_stats": self.sender_stats.to_dict() if self.sender_stats else None,
        }


class UDPSession:
    """
    Manages one bidirectional or one-directional UDP stream session.

    Parameters
    ----------
    session_id   : unique string identifier (e.g. "voice-u3-u7")
    stream_type  : StreamType
    role         : SessionRole
    local_ip     : source IP for this node (bound IP alias)
    peer_ip      : remote node IP
    duration_s   : session duration; None = run until stop() called
    on_snapshot  : optional async or sync callback receiving StreamSnapshot
    stream_id    : numeric stream identifier embedded in packets (default: hash of session_id)
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

    async def run(self) -> SessionSummary:
        """Start the session and block until it ends."""
        profile  = PROFILES[self.stream_type]
        tasks: list[asyncio.Task] = []

        logger.info(
            "Session %s starting: role=%s  type=%s  %s ↔ %s  port=%d",
            self.session_id, self.role.value, self.stream_type.value,
            self.local_ip, self.peer_ip, profile.port,
        )

        # --- Receiver ---
        if self.role in (SessionRole.RECEIVER, SessionRole.BOTH):
            self._receiver = UDPReceiver(
                stream_type     = self.stream_type,
                bind_ip         = self.local_ip,
                on_snapshot     = self._collect_snapshot,
            )
            tasks.append(asyncio.create_task(
                self._receiver.run(), name=f"recv-{self.session_id}"
            ))

        # --- Sender ---
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

        # --- Duration watchdog ---
        if self.duration_s:
            async def _watchdog():
                await asyncio.sleep(self.duration_s)
                self.stop()
            tasks.append(asyncio.create_task(_watchdog(), name=f"watchdog-{self.session_id}"))

        # --- Wait for stop ---
        await self._stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        self._summary.end_time = time.time()
        if self._sender:
            self._summary.sender_stats = self._sender.stats

        logger.info(
            "Session %s ended after %.1fs.  Snapshots: %d",
            self.session_id, self._summary.duration_s, len(self._summary.snapshots),
        )
        return self._summary

    @property
    def summary(self) -> SessionSummary:
        return self._summary
