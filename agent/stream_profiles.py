"""
UDP stream profiles matching Microsoft Teams traffic characteristics.

Reference ports (Teams media):
  Primary STUN/TURN : UDP 3478
  Media fallback    : UDP 3479, 3480, 3481
  Transport relay   : UDP 50000–59999 (we use the low end)

We use three fixed ports for simplicity:
  Voice      → 3478
  Video      → 3479
  Screenshare→ 3480

Packet sizes and intervals are tuned to match real Teams capture data.
"""

from dataclasses import dataclass
from agent.mos import StreamType


@dataclass(frozen=True)
class StreamProfile:
    stream_type: StreamType
    port: int                   # destination UDP port
    packet_size_bytes: int      # payload bytes per packet
    interval_ms: float          # target inter-packet interval (ms)
    dscp: int                   # DSCP marking (for QoS testing)
    label: str

    @property
    def packets_per_second(self) -> float:
        return 1000.0 / self.interval_ms

    @property
    def bitrate_kbps(self) -> float:
        # UDP header (8) + IP header (20) + payload
        return (self.packet_size_bytes + 28) * 8 * self.packets_per_second / 1000.0


VOICE_PROFILE = StreamProfile(
    stream_type    = StreamType.VOICE,
    port           = 3478,
    packet_size_bytes = 160,    # G.711 20 ms frame ≈ 160 bytes
    interval_ms    = 20.0,      # 50 pps → ~67 kbps on wire
    dscp           = 46,        # EF (Expedited Forwarding)
    label          = "voice",
)

VIDEO_PROFILE = StreamProfile(
    stream_type    = StreamType.VIDEO,
    port           = 3479,
    packet_size_bytes = 1200,   # typical RTP video MTU-safe payload
    interval_ms    = 33.3,      # ~30 fps → ~290 kbps on wire
    dscp           = 34,        # AF41
    label          = "video",
)

SCREENSHARE_PROFILE = StreamProfile(
    stream_type    = StreamType.SCREENSHARE,
    port           = 3480,
    packet_size_bytes = 1300,   # slightly larger, screen content
    interval_ms    = 33.3,      # 30 fps baseline; bursty in practice
    dscp           = 18,        # AF21
    label          = "screenshare",
)

PROFILES: dict[StreamType, StreamProfile] = {
    StreamType.VOICE:       VOICE_PROFILE,
    StreamType.VIDEO:       VIDEO_PROFILE,
    StreamType.SCREENSHARE: SCREENSHARE_PROFILE,
}
