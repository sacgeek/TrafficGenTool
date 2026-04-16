"""
NetLab Controller — FastAPI application.

Serves:
  /api/...          REST endpoints for the React dashboard
  /ws/agent         WebSocket endpoint — worker nodes connect here
  /ws/dashboard     WebSocket endpoint — React dashboard live feed
  /                 React SPA (served from ./dashboard/dist)

Start with:
    uvicorn controller.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    HTTPException, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from controller.models import (
    AgentInfo, MsgType, NodeStatus, SessionStatus,
    StreamSnapshot, TestPlan, WSMessage,
)
from controller.session_store import store
from controller.distributor import distribute_plan, PlanError
from controller.export import export_csv, export_pdf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dashboard WebSocket connection manager
# ---------------------------------------------------------------------------

class DashboardConnections:
    """Broadcasts real-time events to all connected dashboard clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("Dashboard client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("Dashboard client disconnected (%d remaining)", len(self._clients))

    async def broadcast(self, event: str, data: dict) -> None:
        if not self._clients:
            return
        msg = json.dumps({"event": event, "data": data})
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


dashboard_bus = DashboardConnections()


# ---------------------------------------------------------------------------
# Agent WebSocket connection registry
# ---------------------------------------------------------------------------

class AgentConnections:
    """Tracks connected agent WebSocket connections by node_id."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}

    def register(self, node_id: str, ws: WebSocket) -> None:
        self._connections[node_id] = ws

    def remove(self, node_id: str) -> None:
        self._connections.pop(node_id, None)

    async def send(self, node_id: str, msg_type: str, payload: dict) -> bool:
        ws = self._connections.get(node_id)
        if not ws:
            logger.warning("No active connection for node %s — cannot send %s", node_id, msg_type)
            return False
        try:
            await ws.send_text(json.dumps({"type": msg_type, "payload": payload}))
            return True
        except Exception as exc:
            logger.warning("Failed to send %s to %s: %s", msg_type, node_id, exc)
            return False

    async def broadcast(self, msg_type: str, payload: dict) -> None:
        for node_id in list(self._connections):
            await self.send(node_id, msg_type, payload)


agent_bus = AgentConnections()


# ---------------------------------------------------------------------------
# Store → Dashboard bridge
# ---------------------------------------------------------------------------

async def _on_store_event(event: str, data: dict) -> None:
    await dashboard_bus.broadcast(event, data)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.subscribe("node_update",    _on_store_event)
    store.subscribe("session_update", _on_store_event)
    store.subscribe("snapshot",       _on_store_event)
    store.subscribe("alert",          _on_store_event)
    store.subscribe("alert_cleared",  _on_store_event)
    logger.info("NetLab controller started.")
    yield
    logger.info("NetLab controller shutting down.")


app = FastAPI(title="NetLab Controller", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Agent WebSocket handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    node_id: str | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = WSMessage.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as exc:
                logger.warning("Bad agent message: %s", exc)
                continue

            # --- REGISTER ---
            if msg.type == MsgType.REGISTER:
                try:
                    info = AgentInfo(**msg.payload)
                except Exception as exc:
                    logger.error("Invalid REGISTER payload: %s", exc)
                    continue
                node_id = info.node_id
                store.register_node(node_id, info)
                store.update_node_status(node_id, NodeStatus.CONNECTED)
                agent_bus.register(node_id, ws)
                await ws.send_text(WSMessage(
                    type=MsgType.PING, payload={"msg": "registered"}
                ).to_json())
                logger.info("Agent registered: %s", node_id)

            # --- PONG (heartbeat reply) ---
            elif msg.type == MsgType.PONG:
                if node_id:
                    store.touch_node(node_id)

            # --- SNAPSHOT ---
            elif msg.type == MsgType.SNAPSHOT:
                if not node_id:
                    continue
                try:
                    snap = StreamSnapshot(
                        node_id=node_id,
                        **msg.payload,
                    )
                    session_id = msg.payload.get("session_id") or ""
                    # Find active session for this node
                    active = store.get_active_session()
                    if active:
                        store.add_snapshot(active.session_id, snap)
                except Exception as exc:
                    logger.error("Bad SNAPSHOT payload from %s: %s", node_id, exc)

            # --- PLAN_STARTED ---
            elif msg.type == MsgType.PLAN_STARTED:
                plan_id = msg.payload.get("plan_id")
                logger.info("Node %s confirmed plan %s started", node_id, plan_id)
                if node_id:
                    store.update_node_status(node_id, NodeStatus.RUNNING)
                    # If all nodes running → mark session as running
                    active = store.get_active_session()
                    if active and active.status == SessionStatus.PENDING:
                        store.start_session(active.session_id)

            # --- PLAN_STOPPED ---
            elif msg.type == MsgType.PLAN_STOPPED:
                plan_id = msg.payload.get("plan_id")
                logger.info("Node %s confirmed plan %s stopped", node_id, plan_id)
                if node_id:
                    store.update_node_status(node_id, NodeStatus.CONNECTED)
                    # If all nodes in the active session are now idle, mark it complete
                    active = store.get_active_session()
                    if active:
                        still_running = [
                            nid for nid in active.node_ids
                            if store.get_node(nid) and store.get_node(nid).status == NodeStatus.RUNNING
                        ]
                        if not still_running:
                            store.complete_session(active.session_id)

            # --- ERROR ---
            elif msg.type == MsgType.ERROR:
                logger.error("Agent %s error: %s", node_id, msg.payload.get("message"))
                if node_id:
                    store.update_node_status(node_id, NodeStatus.ERROR)

    except WebSocketDisconnect:
        logger.info("Agent disconnected: %s", node_id or "unregistered")
    except Exception as exc:
        logger.error("Agent WS error (%s): %s", node_id, exc)
    finally:
        if node_id:
            store.remove_node(node_id)
            agent_bus.remove(node_id)


# ---------------------------------------------------------------------------
# Dashboard WebSocket handler
# ---------------------------------------------------------------------------

@app.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await dashboard_bus.connect(ws)

    # Send current state immediately on connect
    await ws.send_text(json.dumps({
        "event": "init",
        "data": {
            "nodes":    [n.to_dict() for n in store.list_nodes()],
            "sessions": [s.summary_dict() for s in store.list_sessions()],
            "stats":    store.stats(),
        }
    }))

    try:
        while True:
            # Keep connection alive; dashboard sends pings
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"event": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        dashboard_bus.disconnect(ws)


# ---------------------------------------------------------------------------
# Heartbeat task — ping all agents every 15 s
# ---------------------------------------------------------------------------

async def _heartbeat_loop():
    while True:
        await asyncio.sleep(15)
        # Agents that haven't been seen in 45 s are considered offline
        now = time.time()
        for node in store.list_nodes():
            if now - node.last_seen > 45:
                logger.warning("Node %s timed out", node.node_id)
                store.update_node_status(node.node_id, NodeStatus.OFFLINE)


@app.on_event("startup")
async def start_heartbeat():
    asyncio.create_task(_heartbeat_loop())


# ---------------------------------------------------------------------------
# REST API — Nodes
# ---------------------------------------------------------------------------

@app.get("/api/nodes")
async def get_nodes():
    return [n.to_dict() for n in store.list_nodes()]


@app.get("/api/nodes/{node_id}")
async def get_node(node_id: str):
    node = store.get_node(node_id)
    if not node:
        raise HTTPException(404, f"Node {node_id!r} not found")
    return node.to_dict()


# ---------------------------------------------------------------------------
# REST API — YouTube reachability check
# ---------------------------------------------------------------------------

@app.get("/api/check/youtube")
async def check_youtube():
    """
    Quick TCP connect check to www.youtube.com:443.
    Returns {reachable: bool}.
    """
    import socket
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: socket.create_connection(("www.youtube.com", 443), timeout=4)
            ),
            timeout=5,
        )
        return {"reachable": True}
    except Exception:
        return {"reachable": False}


# ---------------------------------------------------------------------------
# REST API — Test plans & sessions
# ---------------------------------------------------------------------------

@app.post("/api/sessions")
async def create_and_run_session(plan: TestPlan):
    """
    Submit a TestPlan.  The controller distributes it to connected nodes
    and returns the new session_id immediately.
    """
    active = store.get_active_session()
    if active:
        raise HTTPException(409, f"Session {active.session_id} is already running. Stop it first.")

    nodes = store.list_nodes()
    idle_nodes = [n for n in nodes if n.status in (NodeStatus.IDLE, NodeStatus.CONNECTED)]

    if not idle_nodes:
        raise HTTPException(503, "No idle worker nodes connected.")

    # Distribute the plan
    try:
        node_plans = distribute_plan(plan, idle_nodes)
    except PlanError as exc:
        raise HTTPException(400, str(exc))

    # Create session
    session = store.create_session(plan, list(node_plans.keys()))

    # Dispatch to agents over their WebSocket connections
    for node_id, node_plan in node_plans.items():
        sent = await agent_bus.send(node_id, "RUN_PLAN", node_plan.model_dump())
        if not sent:
            logger.warning("Could not dispatch RUN_PLAN to %s — not connected?", node_id)

    # Also broadcast to dashboard so the UI can show the plan
    await dashboard_bus.broadcast("plan_dispatched", {
        "session_id": session.session_id,
        "plan_id":    plan.plan_id,
        "node_plans": {nid: np.model_dump() for nid, np in node_plans.items()},
    })

    logger.info(
        "Session %s created and dispatched to %d nodes",
        session.session_id, len(node_plans),
    )
    return {"session_id": session.session_id, "plan_id": plan.plan_id}


@app.get("/api/sessions")
async def list_sessions():
    return [s.summary_dict() for s in store.list_sessions()]


@app.get("/api/sessions/active")
async def get_active_session():
    session = store.get_active_session()
    if not session:
        return None
    return session.summary_dict()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, f"Session {session_id!r} not found")
    return session.full_dict()


@app.get("/api/sessions/{session_id}/snapshots")
async def get_snapshots(
    session_id: str,
    stream_type: str | None = Query(None),
    node_id:     str | None = Query(None),
    limit:       int        = Query(500),
):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    snaps = session.snapshots
    if stream_type:
        snaps = [s for s in snaps if s.stream_type == stream_type]
    if node_id:
        snaps = [s for s in snaps if s.node_id == node_id]
    return [s.to_dict() for s in snaps[-limit:]]


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status not in (SessionStatus.PENDING, SessionStatus.RUNNING):
        raise HTTPException(409, "Session is not running")
    store.stop_session(session_id)
    # Send STOP_PLAN directly to all connected agents
    await agent_bus.broadcast("STOP_PLAN", {"session_id": session_id})
    # Also notify the dashboard
    await dashboard_bus.broadcast("stop_plan", {"session_id": session_id})
    return {"stopped": True}


@app.delete("/api/sessions")
async def clear_sessions():
    count = store.clear_sessions()
    return {"cleared": count}


# ---------------------------------------------------------------------------
# REST API — Export
# ---------------------------------------------------------------------------

@app.get("/api/sessions/{session_id}/export/csv")
async def export_session_csv(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    data = export_csv(session)
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="netlab-{session_id}.csv"'},
    )


@app.get("/api/sessions/{session_id}/export/pdf")
async def export_session_pdf(session_id: str):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    data = export_pdf(session)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="netlab-{session_id}.pdf"'},
    )


# ---------------------------------------------------------------------------
# REST API — Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "server_time": time.time(), **store.stats()}


# ---------------------------------------------------------------------------
# Serve React SPA (production build)
# ---------------------------------------------------------------------------

import os
_DIST = os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
