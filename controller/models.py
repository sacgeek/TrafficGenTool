"""
Shared data models for the NetLab controller.

Key models:
  WSMessage    — WebSocket message envelope (type + payload)
  AgentInfo    — registration data sent by each worker agent
  NodeState    — runtime state of a connected worker node
  TestPlan     — full test plan as submitted from the dashboard
  NodePlan     — per-node slice of a TestPlan dispatched via WebSocket
  StreamSnapshot — telemetry snapshot from one worker stream window
  AlertEvent   — fired when MOS stays below alert_mos_floor for alert_window_s
  TestSession  — one running or completed test run
"""

from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import time
import uuid


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MsgType(str, Enum):
    # Controller → Agent
    PING         = "PING"
    RUN_PLAN     = "RUN_PLAN"
    STOP_PLAN    = "STOP_PLAN"

    # Agent → Controller
    REGISTER     = "REGISTER"
    PONG         = "PONG"
    SNAPSHOT     = "SNAPSHOT"
    PLAN_STARTED = "PLAN_STARTED"
    PLAN_STOPPED = "PLAN_STOPPED"
    ERROR        = "ERROR"


class StreamType(str, Enum):
    VOICE       = "voice"
    VIDEO       = "video"
    SCREENSHARE = "screenshare"
    WEB         = "web"
    YOUTUBE     = "youtube"


class NodeStatus(str, Enum):
    CONNECTED = "connected"
    RUNNING   = "running"
    IDLE      = "idle"
    ERROR     = "error"
    OFFLINE   = "offline"


class SessionStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    STOPPED  = "stopped"
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# WebSocket message envelope
# ---------------------------------------------------------------------------

class WSMessage(BaseModel):
    type:    MsgType
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

class AgentInfo(BaseModel):
    node_id:       str
    hostname:      str
    ip_range_start: str
    ip_range_end:   str
    max_users:     int = 20
    capabilities:  list[str] = Field(default_factory=lambda: [
        "voice", "video", "screenshare", "web", "youtube"
    ])
    agent_version: str = "1.0.0"
    clock_delta_ms: float | None = None  # measured at REGISTER time; None = check failed


class NodeState(BaseModel):
    """Runtime state of a connected worker node (held in memory)."""
    node_id:    str
    info:       AgentInfo
    status:     NodeStatus = NodeStatus.IDLE
    connected_at: float = Field(default_factory=time.time)
    last_seen:  float    = Field(default_factory=time.time)
    current_plan_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "node_id":        self.node_id,
            "hostname":       self.info.hostname,
            "ip_range":       f"{self.info.ip_range_start} – {self.info.ip_range_end}",
            "max_users":      self.info.max_users,
            "capabilities":   self.info.capabilities,
            "status":         self.status.value,
            "connected_at":   self.connected_at,
            "last_seen":      self.last_seen,
            "current_plan_id": self.current_plan_id,
            "clock_delta_ms": self.info.clock_delta_ms,  # None = check failed at registration
        }


# ---------------------------------------------------------------------------
# Test plan
# ---------------------------------------------------------------------------

class UserPairAssignment(BaseModel):
    """One simulated user's call assignment across two nodes."""
    user_index:   int
    src_node_id:  str
    src_ip:       str
    dst_node_id:  str
    dst_ip:       str


class TestPlan(BaseModel):
    """
    A complete test plan as submitted from the dashboard.
    The controller distributes this to each participating node.
    """
    plan_id:      str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name:         str = "Unnamed test"

    # Web browsing
    web_users:    int = 0
    web_urls:     list[str] = Field(default_factory=list)

    # YouTube streaming
    youtube_users: int = 0
    youtube_url:   str = ""   # ← NEW: empty = worker uses its built-in default

    # UDP call simulation
    voice_calls:      int = 0
    video_calls:      int = 0
    screen_shares:    int = 0

    # Duration
    duration_s:   int = 60      # 0 = run until manually stopped

    # Alert thresholds (controller-side, not forwarded to agents)
    alert_mos_floor: float = 3.5  # MOS below this triggers an alert (0 = disabled)
    alert_window_s:  int   = 10   # seconds of sustained low MOS before alert fires

    # Assigned nodes (filled by controller before dispatch)
    node_ids:     list[str] = Field(default_factory=list)

    created_at:   float = Field(default_factory=time.time)


class NodePlan(BaseModel):
    """
    The slice of a TestPlan sent to a specific worker node.
    Contains only this node's assignments.
    """
    plan_id:      str
    node_id:      str
    duration_s:   int

    # Web / YouTube workers for this node
    web_users:    int = 0
    web_urls:     list[str] = Field(default_factory=list)
    youtube_users: int = 0
    youtube_url:   str = ""   # ← NEW: forwarded from TestPlan.youtube_url

    # UDP sessions: list of (session_id, type, role, local_ip, peer_ip)
    udp_sessions: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Telemetry / snapshots
# ---------------------------------------------------------------------------

class StreamSnapshot(BaseModel):
    """
    Telemetry snapshot from a worker node for one stream window.
    Mirrors agent.workers.udp_receiver.StreamSnapshot as a Pydantic model.
    """
    node_id:         str
    session_id:      str
    stream_type:     StreamType
    timestamp:       float
    packets_recv:    int = 0
    packets_expected: int = 0
    loss_pct:        float = 0.0
    latency_ms:      float = 0.0
    jitter_ms:       float = 0.0
    mos_mos:         float = 0.0
    mos_label:       str   = "unknown"
    mos_color:       str   = "gray"
    mos_r_factor:    float = 0.0

    # Web/YouTube specific
    page_load_ms:    float | None = None
    bytes_downloaded: int | None = None

    def to_dict(self) -> dict:
        return self.model_dump()


# ---------------------------------------------------------------------------
# Alert events
# ---------------------------------------------------------------------------

class AlertEvent(BaseModel):
    """
    Fired when a stream's MOS stays below alert_mos_floor for alert_window_s
    seconds.  Lives in-memory on TestSession.alerts.  cleared_at is set (to
    a Unix timestamp) when MOS recovers above the floor; while None the alert
    is considered active and shown in the dashboard banner.
    """
    alert_id:    str   = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    session_id:  str
    stream_key:  str          # "{node_id}:{worker_session_id}"
    stream_type: str
    node_id:     str
    mos_at_alert: float       # MOS reading that crossed the window
    floor:       float        # threshold that was configured
    fired_at:    float = Field(default_factory=time.time)
    cleared_at:  float | None = None

    @property
    def is_active(self) -> bool:
        return self.cleared_at is None

    def to_dict(self) -> dict:
        d = self.model_dump()
        d["is_active"] = self.is_active
        return d


# ---------------------------------------------------------------------------
# Session (one test run)
# ---------------------------------------------------------------------------

class TestSession(BaseModel):
    """
    A running or completed test session (one execution of a TestPlan).
    """
    session_id:  str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    plan:        TestPlan
    status:      SessionStatus = SessionStatus.PENDING
    start_time:  float | None = None
    end_time:    float | None = None
    snapshots:   list[StreamSnapshot] = Field(default_factory=list)
    alerts:      list[AlertEvent]     = Field(default_factory=list)
    node_ids:    list[str] = Field(default_factory=list)

    @property
    def duration_s(self) -> float | None:
        if self.start_time is None:
            return None
        end = self.end_time or time.time()
        return end - self.start_time

    def add_snapshot(self, snap: StreamSnapshot) -> None:
        self.snapshots.append(snap)

    def summary_dict(self) -> dict:
        active_alerts = sum(1 for a in self.alerts if a.is_active)
        return {
            "session_id":    self.session_id,
            "plan_name":     self.plan.name,
            "status":        self.status.value,
            "start_time":    self.start_time,
            "end_time":      self.end_time,
            "duration_s":    self.duration_s,
            "node_ids":      self.node_ids,
            "snapshot_count": len(self.snapshots),
            "active_alerts": active_alerts,
        }

    def full_dict(self) -> dict:
        return {
            **self.summary_dict(),
            "plan":      self.plan.model_dump(),
            "snapshots": [s.to_dict() for s in self.snapshots],
            "alerts":    [a.to_dict() for a in self.alerts],
        }
