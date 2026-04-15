"""
MOS (Mean Opinion Score) calculation engine.

Implements a simplified ITU-T G.107 E-model to derive a MOS score (1.0–5.0)
from measured packet loss, one-way latency, and jitter for each stream type.

Stream type profiles (bitrate, codec assumptions):
  voice      : ~50–100 kbps, G.711/Opus, 20 ms packetisation
  video      : ~500 kbps–2 Mbps, VP8/H.264, 33 ms packetisation
  screenshare: ~1–4 Mbps, bursty, 33 ms packetisation
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from enum import Enum


class StreamType(str, Enum):
    VOICE = "voice"
    VIDEO = "video"
    SCREENSHARE = "screenshare"


# Per-stream-type weighting factors for how much loss/latency/jitter
# degrades the experience.  Voice is most sensitive to latency; video
# tolerates slightly more latency but is more sensitive to burst loss.
_PROFILE = {
    StreamType.VOICE: {
        "ie": 0,           # equipment impairment factor (codec baseline)
        "bpl": 25.1,       # burst packet loss robustness
        "latency_weight": 1.0,
        "jitter_weight":  1.2,
        "loss_weight":    1.0,
    },
    StreamType.VIDEO: {
        "ie": 5,
        "bpl": 20.0,
        "latency_weight": 0.7,
        "jitter_weight":  1.0,
        "loss_weight":    1.3,
    },
    StreamType.SCREENSHARE: {
        "ie": 8,
        "bpl": 15.0,
        "latency_weight": 0.5,
        "jitter_weight":  0.8,
        "loss_weight":    1.5,
    },
}

# Thresholds for user-experience labels
MOS_EXCELLENT  = 4.3
MOS_GOOD       = 4.0
MOS_FAIR       = 3.6
MOS_POOR       = 3.1
# below POOR → "bad"


@dataclass
class MOSResult:
    mos: float                   # 1.0 – 5.0
    r_factor: float              # underlying R-value (0–100)
    label: str                   # "excellent" | "good" | "fair" | "poor" | "bad"
    loss_pct: float
    latency_ms: float
    jitter_ms: float
    stream_type: StreamType

    @property
    def color(self) -> str:
        """Traffic-light color for UI display."""
        if self.mos >= MOS_GOOD:
            return "green"
        if self.mos >= MOS_FAIR:
            return "yellow"
        return "red"

    def to_dict(self) -> dict:
        return {
            "mos":         round(self.mos, 2),
            "r_factor":    round(self.r_factor, 1),
            "label":       self.label,
            "color":       self.color,
            "loss_pct":    round(self.loss_pct, 2),
            "latency_ms":  round(self.latency_ms, 1),
            "jitter_ms":   round(self.jitter_ms, 1),
            "stream_type": self.stream_type.value,
        }


def calculate_mos(
    loss_pct: float,
    latency_ms: float,
    jitter_ms: float,
    stream_type: StreamType = StreamType.VOICE,
) -> MOSResult:
    """
    Compute MOS from raw stream statistics.

    Parameters
    ----------
    loss_pct   : packet loss as a percentage (0–100)
    latency_ms : one-way latency in milliseconds
    jitter_ms  : jitter (inter-arrival variance) in milliseconds
    stream_type: StreamType enum value

    Returns
    -------
    MOSResult dataclass
    """
    loss_pct   = max(0.0, min(100.0, loss_pct))
    latency_ms = max(0.0, latency_ms)
    jitter_ms  = max(0.0, jitter_ms)

    profile = _PROFILE[stream_type]

    # --- Delay impairment (Id) ---
    # Effective latency includes jitter buffer (est. ~2× jitter) and codec delay.
    # We add a codec/processing constant of 10 ms.
    effective_latency = latency_ms + (jitter_ms * 2.0) + 10.0

    if effective_latency < 160:
        id_factor = 0.0
    elif effective_latency < 400:
        id_factor = 0.024 * effective_latency + 0.11 * (effective_latency - 177.3) * max(0, effective_latency - 177.3) / 1000.0
    else:
        id_factor = 0.024 * effective_latency + 0.11 * (effective_latency - 177.3) * (effective_latency - 177.3) / 1000.0

    id_factor *= profile["latency_weight"]

    # --- Jitter impairment (separate contribution) ---
    # Jitter above ~30 ms starts causing noticeable degradation.
    jitter_penalty = max(0.0, (jitter_ms - 30.0) * 0.15) * profile["jitter_weight"]

    # --- Packet loss impairment (Ie-eff) ---
    # E-model effective equipment impairment including loss.
    ie  = profile["ie"]
    bpl = profile["bpl"]
    if loss_pct > 0:
        ie_eff = ie + (95 - ie) * (loss_pct / (loss_pct + bpl))
    else:
        ie_eff = float(ie)

    ie_eff *= profile["loss_weight"]

    # --- R-factor ---
    # R = R0 - Is - Id - Ie_eff + A
    # R0 = 93.2 (base signal-to-noise), Is = 1.4 (simultaneous impairment),
    # A  = 0    (no advantage factor for lab testing)
    r = 93.2 - 1.4 - id_factor - jitter_penalty - ie_eff
    r = max(0.0, min(100.0, r))

    # --- R → MOS conversion (ITU-T G.107) ---
    if r < 0:
        mos = 1.0
    elif r > 100:
        mos = 4.5
    else:
        mos = 1.0 + 0.035 * r + r * (r - 60.0) * (100.0 - r) * 7e-6

    mos = max(1.0, min(5.0, mos))

    # --- Label ---
    if mos >= MOS_EXCELLENT:
        label = "excellent"
    elif mos >= MOS_GOOD:
        label = "good"
    elif mos >= MOS_FAIR:
        label = "fair"
    elif mos >= MOS_POOR:
        label = "poor"
    else:
        label = "bad"

    return MOSResult(
        mos=mos,
        r_factor=r,
        label=label,
        loss_pct=loss_pct,
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        stream_type=stream_type,
    )


def aggregate_mos(results: list[MOSResult]) -> MOSResult | None:
    """
    Average multiple MOSResult objects into a single summary score.
    Uses the stream_type of the first result as the label type.
    Returns None for an empty list.
    """
    if not results:
        return None
    avg_loss    = sum(r.loss_pct   for r in results) / len(results)
    avg_latency = sum(r.latency_ms for r in results) / len(results)
    avg_jitter  = sum(r.jitter_ms  for r in results) / len(results)
    return calculate_mos(avg_loss, avg_latency, avg_jitter, results[0].stream_type)
