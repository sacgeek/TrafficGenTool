"""
UDP stream profiles matching Microsoft Teams traffic characteristics.

Reference ports (Teams media):
  Primary STUN/TURN : UDP 3478
  Media fallback    : UDP 3479, 3480, 3481
  Transport relay   : UDP 50000–59999 (we use the low end)

Destination ports (fixed, all fall within valid Teams ranges):
  Voice      → 3478
  Video      → 3479
  Screenshare→ 3480

Source port ranges (per Microsoft spec, used by the application detection
engine on routers to classify traffic by stream type):
  Teams-Audio   : 50000–50019
  Teams-Video   : 50020–50039
  Teams-Sharing : 50040–50059

Packet sizes and intervals are tuned to match real Teams capture data.
"""

import random
from dataclasses import dataclass
from agent.mos import StreamType


@dataclass(frozen=True)
class StreamProfile:
    stream_type: StreamType
    port: int                        # destination UDP port
    src_port_range: tuple[int, int]  # (min, max) inclusive source port range
    packet_size_bytes: int           # payload bytes per packet
    interval_ms: float               # target inter-packet interval (ms)
    dscp: int                        # DSCP marking (for QoS testing)
    label: str
    responder_rate_ratio: float = 1.0  # fraction of initiator rate that RESPONDER sends back
                                       # 1.0 = symmetric (voice, video); 0.1 = 10:1 (screenshare)

    def pick_src_port(self) -> int:
        """Return a random source port from this stream type's assigned range."""
        return random.randint(self.src_port_range[0], self.src_port_range[1])

    @property
    def packets_per_second(self) -> float:
        return 1000.0 / self.interval_ms

    @property
    def bitrate_kbps(self) -> float:
        # UDP header (8) + IP header (20) + payload
        return (self.packet_size_bytes + 28) * 8 * self.packets_per_second / 1000.0


VOICE_PROFILE = StreamProfile(
    stream_type       = StreamType.VOICE,
    port              = 3478,
    src_port_range    = (50000, 50019),  # Teams-Audio source range
    packet_size_bytes = 160,             # G.711 20 ms frame ≈ 160 bytes
    interval_ms       = 20.0,            # 50 pps → ~67 kbps on wire
    dscp              = 46,              # EF (Expedited Forwarding)
    label             = "voice",
)

VIDEO_PROFILE = StreamProfile(
    stream_type       = StreamType.VIDEO,
    port              = 3479,
    src_port_range    = (50020, 50039),  # Teams-Video source range
    packet_size_bytes = 1200,            # typical RTP video MTU-safe payload
    interval_ms       = 33.3,            # ~30 fps → ~290 kbps on wire
    dscp              = 34,              # AF41
    label             = "video",
)

SCREENSHARE_PROFILE = StreamProfile(
    stream_type          = StreamType.SCREENSHARE,
    port                 = 3480,
    src_port_range       = (50040, 50059),  # Teams-Sharing source range
    packet_size_bytes    = 1300,            # slightly larger, screen content
    interval_ms          = 33.3,            # 30 fps baseline; bursty in practice
    dscp                 = 18,              # AF21
    label                = "screenshare",
    responder_rate_ratio = 0.1,             # viewers send ~3 fps of RTCP/control back (10:1 ratio)
)

PROFILES: dict[StreamType, StreamProfile] = {
    StreamType.VOICE:       VOICE_PROFILE,
    StreamType.VIDEO:       VIDEO_PROFILE,
    StreamType.SCREENSHARE: SCREENSHARE_PROFILE,
}
