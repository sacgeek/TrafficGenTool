"""
UDP stream sender worker.

Each sender represents one simulated user's outbound UDP stream.
Packets carry a lightweight header so the receiver can compute:
  - sequence gaps  → packet loss
  - send→receive delta → one-way latency (requires clock sync or NTP)
  - inter-arrival variance → jitter

Packet wire format (24-byte header + padding payload):
  Offset  Size  Field
  0       4     magic (0xNETLAB01 = 0x4E544C01)
  4       4     stream_id  (uint32, identifies this sender session)
  8       8     sequence   (uint64, monotonically increasing)
  16      8     send_ts_us (uint64, microseconds since Unix epoch)
  [24 …]        zero-padded payload to reach target packet_size_bytes
"""

from __future__ import annotations
import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field

from agent.stream_profiles import StreamProfile, PROFILES
from agent.mos import StreamType

logger = logging.getLogger(__name__)

HEADER_MAGIC  = 0x4E544C01
HEADER_FORMAT = "!IIQq"          # big-endian: uint32 uint32 uint64 int64
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)   # == 24 bytes


@dataclass
class SenderStats:
    stream_id:    int
    stream_type:  StreamType
    packets_sent: int = 0
    bytes_sent:   int = 0
    start_time:   float = field(default_factory=time.monotonic)
    stop_time:    float | None = None

    @property
    def elapsed_seconds(self) -> float:
        end = self.stop_time or time.monotonic()
        return end - self.start_time

    @property
    def actual_pps(self) -> float:
        if self.elapsed_seconds == 0:
            return 0.0
        return self.packets_sent / self.elapsed_seconds

    def to_dict(self) -> dict:
        return {
            "stream_id":    self.stream_id,
            "stream_type":  self.stream_type.value,
            "packets_sent": self.packets_sent,
            "bytes_sent":   self.bytes_sent,
            "elapsed_s":    round(self.elapsed_seconds, 2),
            "actual_pps":   round(self.actual_pps, 1),
        }


class UDPSender:
    """
    Sends a UDP stream from src_ip:src_port → dst_ip:profile.port.

    Parameters
    ----------
    stream_id   : unique integer identifying this stream session
    stream_type : StreamType enum
    src_ip      : source IP to bind (one of the node's IP aliases)
    dst_ip      : destination IP (peer worker node)
    src_port    : override source port (defaults to a random port from profile.src_port_range)
    dst_port    : override destination port (defaults to profile.port)
    duration_s  : how long to send; None = run until stop() called
    """

    def __init__(
        self,
        stream_id:   int,
        stream_type: StreamType,
        src_ip:      str,
        dst_ip:      str,
        src_port:    int | None = None,
        dst_port:    int | None = None,
        duration_s:  float | None = None,
    ) -> None:
        self.stream_id   = stream_id
        self.stream_type = stream_type
        self.src_ip      = src_ip
        self.dst_ip      = dst_ip
        self.profile: StreamProfile = PROFILES[stream_type]
        self.src_port    = src_port or self.profile.pick_src_port()
        self.dst_port    = dst_port or self.profile.port
        self.duration_s  = duration_s
        self._stop_event = asyncio.Event()
        self.stats       = SenderStats(stream_id=stream_id, stream_type=stream_type)
        self._transport  = None
        self._protocol   = None

    def stop(self) -> None:
        self._stop_event.set()

    def _build_packet(self, seq: int) -> bytes:
        ts_us = int(time.time() * 1_000_000)
        header = struct.pack(HEADER_FORMAT, HEADER_MAGIC, self.stream_id, seq, ts_us)
        padding_needed = max(0, self.profile.packet_size_bytes - HEADER_SIZE)
        return header + bytes(padding_needed)

    async def run(self) -> SenderStats:
        """
        Main send loop.  Returns SenderStats when the stream ends.
        Binds to src_ip so packets carry the correct source address.
        """
        loop = asyncio.get_running_loop()

        # Create a UDP socket bound to our assigned IP alias and Teams source port
        transport, protocol = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=(self.src_ip, self.src_port),
            remote_addr=(self.dst_ip, self.dst_port),
            family=10 if ":" in self.src_ip else 2,  # AF_INET6 or AF_INET
        )
        self._transport = transport

        logger.info(
            "Sender %d started: %s:%d → %s:%d  type=%s  %.0f pps  %.0f kbps",
            self.stream_id, self.src_ip, self.src_port, self.dst_ip, self.dst_port,
            self.stream_type.value,
            self.profile.packets_per_second,
            self.profile.bitrate_kbps,
        )

        seq          = 0
        interval_s   = self.profile.interval_ms / 1000.0
        deadline     = time.monotonic() + (self.duration_s or float("inf"))
        next_send    = time.monotonic()

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= deadline:
                    break

                if now >= next_send:
                    pkt = self._build_packet(seq)
                    transport.sendto(pkt)
                    self.stats.packets_sent += 1
                    self.stats.bytes_sent   += len(pkt)
                    seq       += 1
                    next_send += interval_s
                else:
                    # Sleep until the next packet is due (busy-wait last 0.5 ms)
                    sleep_s = next_send - now - 0.0005
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
        finally:
            transport.close()
            self.stats.stop_time = time.monotonic()
            logger.info(
                "Sender %d stopped: sent %d packets in %.1fs",
                self.stream_id, self.stats.packets_sent, self.stats.elapsed_seconds,
            )

        return self.stats
