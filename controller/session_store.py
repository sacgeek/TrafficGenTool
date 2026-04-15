"""
In-memory session store.

Holds all runtime state for the controller:
  - Connected worker nodes
  - Active and completed test sessions
  - Per-session snapshot history

No database — state lives in memory for the lifetime of the controller
process.  The export module serialises snapshots to CSV/PDF on demand.

Thread/async safety: all mutations go through the store's async methods,
which run in the single asyncio event loop.  No locking needed.
"""

from __future__ import annotations
import logging
import time
from collections import defaultdict
from typing import Callable

from controller.models import (
    NodeState, NodeStatus, TestSession, TestPlan, StreamSnapshot,
    SessionStatus, AgentInfo, AlertEvent,
)

logger = logging.getLogger(__name__)

# Maximum snapshots retained per session (prevents unbounded growth for
# long-running tests; oldest are dropped when limit is reached).
MAX_SNAPSHOTS_PER_SESSION = 50_000


class SessionStore:
    """
    Central in-memory store.  One instance per controller process.
    """

    def __init__(self) -> None:
        # node_id → NodeState
        self._nodes: dict[str, NodeState] = {}

        # session_id → TestSession
        self._sessions: dict[str, TestSession] = {}

        # Change listeners for WebSocket broadcast
        # key → list of async callables that accept an event dict
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

        # Alert tracking state (in-memory, cleared with session)
        # session_id → { stream_key → Unix timestamp when MOS first dropped below floor }
        self._alert_below_since: dict[str, dict[str, float]] = defaultdict(dict)
        # session_id → set of stream_keys for which an alert has already fired
        #              (prevents repeated firing while still below floor)
        self._alert_fired: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def register_node(self, node_id: str, info: AgentInfo) -> NodeState:
        node = NodeState(node_id=node_id, info=info)
        self._nodes[node_id] = node
        logger.info("Node registered: %s (%s)", node_id, info.hostname)
        self._emit("node_update", node.to_dict())
        return node

    def get_node(self, node_id: str) -> NodeState | None:
        return self._nodes.get(node_id)

    def list_nodes(self) -> list[NodeState]:
        return list(self._nodes.values())

    def update_node_status(self, node_id: str, status: NodeStatus) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.status = status
            node.last_seen = time.time()
            self._emit("node_update", node.to_dict())

    def touch_node(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.last_seen = time.time()

    def remove_node(self, node_id: str) -> None:
        node = self._nodes.pop(node_id, None)
        if node:
            logger.info("Node disconnected: %s", node_id)
            self._emit("node_update", {"node_id": node_id, "status": "offline"})

    def connected_node_ids(self) -> list[str]:
        return [
            nid for nid, n in self._nodes.items()
            if n.status != NodeStatus.OFFLINE
        ]

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self, plan: TestPlan, node_ids: list[str]) -> TestSession:
        session = TestSession(plan=plan, node_ids=node_ids)
        self._sessions[session.session_id] = session
        logger.info(
            "Session created: %s  plan=%s  nodes=%s",
            session.session_id, plan.plan_id, node_ids,
        )
        self._emit("session_update", session.summary_dict())
        return session

    def get_session(self, session_id: str) -> TestSession | None:
        return self._sessions.get(session_id)

    def get_active_session(self) -> TestSession | None:
        for s in self._sessions.values():
            if s.status in (SessionStatus.PENDING, SessionStatus.RUNNING):
                return s
        return None

    def list_sessions(self) -> list[TestSession]:
        return sorted(
            self._sessions.values(),
            key=lambda s: s.start_time or 0,
            reverse=True,
        )

    def start_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.status = SessionStatus.RUNNING
            session.start_time = time.time()
            logger.info("Session started: %s", session_id)
            self._emit("session_update", session.summary_dict())

    def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.status = SessionStatus.STOPPED
            session.end_time = time.time()
            logger.info("Session stopped: %s", session_id)
            self._emit("session_update", session.summary_dict())
            # Mark all participating nodes as idle
            for nid in session.node_ids:
                self.update_node_status(nid, NodeStatus.IDLE)
            self._clear_alert_state(session_id)

    def complete_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.status = SessionStatus.COMPLETE
            session.end_time = time.time()
            logger.info("Session complete: %s", session_id)
            self._emit("session_update", session.summary_dict())
            for nid in session.node_ids:
                self.update_node_status(nid, NodeStatus.IDLE)
            self._clear_alert_state(session_id)

    def clear_sessions(self) -> int:
        """Remove all completed/stopped sessions. Returns count removed."""
        to_remove = [
            sid for sid, s in self._sessions.items()
            if s.status in (SessionStatus.COMPLETE, SessionStatus.STOPPED)
        ]
        for sid in to_remove:
            del self._sessions[sid]
            self._clear_alert_state(sid)
        logger.info("Cleared %d sessions", len(to_remove))
        return len(to_remove)

    # ------------------------------------------------------------------
    # Snapshot ingestion
    # ------------------------------------------------------------------

    def add_snapshot(self, session_id: str, snapshot: StreamSnapshot) -> None:
        session = self._sessions.get(session_id)
        if not session:
            logger.warning("Snapshot for unknown session %s", session_id)
            return

        # Trim oldest if at cap
        if len(session.snapshots) >= MAX_SNAPSHOTS_PER_SESSION:
            session.snapshots = session.snapshots[-(MAX_SNAPSHOTS_PER_SESSION // 2):]

        session.add_snapshot(snapshot)
        self._emit("snapshot", snapshot.to_dict())

        # --- Alert threshold tracking ---
        self._check_alert(session, snapshot)

    def _check_alert(self, session: "TestSession", snapshot: StreamSnapshot) -> None:
        """
        Fire an alert when a stream's MOS stays below alert_mos_floor for
        alert_window_s seconds.  Clear it when MOS recovers.
        MOS == 0.0 is treated as "no data" and ignored.
        """
        floor  = session.plan.alert_mos_floor
        window = session.plan.alert_window_s

        # Alerts disabled or no data
        if floor <= 0.0 or snapshot.mos_mos <= 0.0:
            return

        session_id = session.session_id
        stream_key = f"{snapshot.node_id}:{snapshot.session_id}"
        now        = time.time()
        below      = self._alert_below_since[session_id]
        fired      = self._alert_fired[session_id]

        if snapshot.mos_mos < floor:
            if stream_key not in below:
                # MOS just dropped below floor — start the clock
                below[stream_key] = now
            elif stream_key not in fired:
                # Still below floor — check if window has elapsed
                if now - below[stream_key] >= window:
                    fired.add(stream_key)
                    alert = AlertEvent(
                        session_id   = session_id,
                        stream_key   = stream_key,
                        stream_type  = snapshot.stream_type.value
                                       if hasattr(snapshot.stream_type, "value")
                                       else str(snapshot.stream_type),
                        node_id      = snapshot.node_id,
                        mos_at_alert = snapshot.mos_mos,
                        floor        = floor,
                    )
                    session.alerts.append(alert)
                    self._emit("alert", alert.to_dict())
                    logger.warning(
                        "ALERT: stream %s on %s  MOS=%.2f < floor=%.1f  (%.0fs)",
                        stream_key, session_id, snapshot.mos_mos, floor,
                        now - below[stream_key],
                    )
        else:
            # MOS recovered above floor
            if stream_key in below:
                del below[stream_key]
            if stream_key in fired:
                fired.discard(stream_key)
                # Find the most recent active alert for this stream and clear it
                for alert in reversed(session.alerts):
                    if alert.stream_key == stream_key and alert.is_active:
                        alert.cleared_at = now
                        self._emit("alert_cleared", {
                            "alert_id":   alert.alert_id,
                            "session_id": session_id,
                            "stream_key": stream_key,
                            "cleared_at": now,
                        })
                        logger.info(
                            "Alert cleared: stream %s on %s  MOS=%.2f",
                            stream_key, session_id, snapshot.mos_mos,
                        )
                        break

    def _clear_alert_state(self, session_id: str) -> None:
        """Remove in-memory alert tracking for a finished session."""
        self._alert_below_since.pop(session_id, None)
        self._alert_fired.pop(session_id, None)

    # ------------------------------------------------------------------
    # Change listener (for WebSocket broadcast to dashboard)
    # ------------------------------------------------------------------

    def subscribe(self, event: str, callback: Callable) -> None:
        self._listeners[event].append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        try:
            self._listeners[event].remove(callback)
        except ValueError:
            pass

    def _emit(self, event: str, data: dict) -> None:
        import asyncio
        for cb in list(self._listeners.get(event, [])):
            try:
                result = cb(event, data)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception as exc:
                logger.error("Listener error for event %s: %s", event, exc)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "nodes_connected": len(self._nodes),
            "sessions_total":  len(self._sessions),
            "sessions_active": sum(
                1 for s in self._sessions.values()
                if s.status == SessionStatus.RUNNING
            ),
            "snapshots_total": sum(
                len(s.snapshots) for s in self._sessions.values()
            ),
        }


# Module-level singleton
store = SessionStore()
