"""
NetLab Agent — worker node process.

Usage:
    python3 -m agent.agent \
        --controller ws://192.168.1.10:8000/ws/agent \
        --node-id worker-node-1 \
        --ip-start 192.168.1.101 \
        --ip-end   192.168.1.120 \
        --interface eth0

The agent:
  1. Connects to the controller WebSocket and sends REGISTER.
  2. Waits for RUN_PLAN messages.
  3. Provisions IP aliases, spawns UDP/web/youtube workers.
  4. Streams SNAPSHOT telemetry every 2 seconds back to the controller.
  5. Responds to STOP_PLAN and tears down all workers and aliases.
  6. Reconnects automatically if the controller drops the connection.
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import platform
import signal
import socket
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WebSocket client (uses websockets library; falls back to basic if absent)
# ---------------------------------------------------------------------------

try:
    import websockets
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False
    logger.warning("websockets library not installed — agent will run in simulation mode")


# ---------------------------------------------------------------------------
# Agent config
# ---------------------------------------------------------------------------

class AgentConfig:
    def __init__(
        self,
        controller_url: str,
        node_id:        str,
        ip_start:       str,
        ip_end:         str,
        interface:      str = "eth0",
        max_users:      int = 20,
    ) -> None:
        self.controller_url = controller_url
        self.node_id        = node_id
        self.ip_start       = ip_start
        self.ip_end         = ip_end
        self.interface      = interface
        self.max_users      = max_users
        self.hostname       = socket.gethostname()


# ---------------------------------------------------------------------------
# Plan executor
# ---------------------------------------------------------------------------

class PlanExecutor:
    """
    Receives a NodePlan dict and orchestrates all workers for this node.
    """

    def __init__(self, config: AgentConfig, on_snapshot) -> None:
        self.config      = config
        self.on_snapshot = on_snapshot
        self._tasks: list[asyncio.Task] = []
        self._ip_mgr = None
        self._stop_event = asyncio.Event()

    async def run(self, node_plan: dict) -> None:
        from agent.ip_manager import IPAliasManager
        from agent.workers.udp_session import UDPSession, SessionRole
        from agent.mos import StreamType

        plan_id    = node_plan.get("plan_id", "unknown")
        duration_s = node_plan.get("duration_s", 0) or None

        logger.info("PlanExecutor: starting plan %s", plan_id)
        self._stop_event.clear()

        # 1. Provision IP aliases
        self._ip_mgr = IPAliasManager(
            ip_range_start = self.config.ip_start,
            ip_range_end   = self.config.ip_end,
            interface      = self.config.interface,
        )
        provisioned = await self._ip_mgr.provision_all()
        logger.info("Provisioned %d IP aliases", len(provisioned))

        # 2. Start UDP sessions
        for sess_cfg in node_plan.get("udp_sessions", []):
            try:
                stream_type = StreamType(sess_cfg["stream_type"])
                role_str    = sess_cfg.get("role", "both")

                role_map = {
                    "both":     SessionRole.BOTH,
                    "sender":   SessionRole.SENDER,
                    "receiver": SessionRole.RECEIVER,
                }
                role = role_map.get(role_str, SessionRole.BOTH)

                # For one-to-many screenshare sender, spawn one session per peer
                peer_ips = sess_cfg.get("peer_ips") or (
                    [sess_cfg["peer_ip"]] if "peer_ip" in sess_cfg else []
                )

                for peer_ip in (peer_ips if peer_ips else [sess_cfg.get("peer_ip", "")]):
                    if not peer_ip:
                        continue
                    session = UDPSession(
                        session_id  = sess_cfg["session_id"],
                        stream_type = stream_type,
                        role        = role,
                        local_ip    = sess_cfg["local_ip"],
                        peer_ip     = peer_ip,
                        duration_s  = float(duration_s) if duration_s else None,
                        on_snapshot = self._wrap_snapshot(sess_cfg["session_id"], stream_type),
                    )
                    self._tasks.append(
                        asyncio.create_task(session.run(), name=f"udp-{sess_cfg['session_id']}")
                    )
            except Exception as exc:
                logger.error("Failed to start UDP session %s: %s", sess_cfg.get("session_id"), exc)

        # 3. Start web workers
        web_users = node_plan.get("web_users", 0)
        web_urls  = node_plan.get("web_urls", [])
        if web_users > 0 and web_urls:
            try:
                from agent.workers.web_worker import WebWorker
                avail_ips = self._ip_mgr.available_ips
                for i in range(web_users):
                    local_ip  = avail_ips[i % len(avail_ips)]
                    worker_id = f"web-{plan_id}-u{i}"

                    # Capture loop vars in closure
                    def _make_web_cb(wid: str, lip: str):
                        def _cb(snap) -> None:
                            try:
                                d = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap)
                                d["session_id"]  = wid
                                d["stream_type"] = "web"
                                d["node_id"]     = self.config.node_id
                                asyncio.ensure_future(self.on_snapshot(d))
                            except Exception as exc:
                                logger.error("Web snapshot callback error: %s", exc)
                        return _cb

                    worker = WebWorker(
                        worker_id   = worker_id,
                        local_ip    = local_ip,
                        urls        = web_urls,
                        duration_s  = float(duration_s) if duration_s else None,
                        on_snapshot = _make_web_cb(worker_id, local_ip),
                    )
                    self._tasks.append(
                        asyncio.create_task(worker.run(), name=f"web-{worker_id}")
                    )
                logger.info("Started %d web worker(s) across %d URL(s)", web_users, len(web_urls))
            except ImportError:
                logger.error(
                    "WebWorker not available — ensure agent/workers/web_worker.py is present "
                    "and aiohttp is installed (pip install aiohttp)"
                )

        # 4. YouTube workers
        yt_users = node_plan.get("youtube_users", 0)
        yt_url   = node_plan.get("youtube_url", "") or ""
        if yt_users > 0:
            try:
                from agent.workers.youtube_worker import YoutubeWorker, DEFAULT_URL
                avail_ips = self._ip_mgr.available_ips
                effective_url = yt_url or DEFAULT_URL
                for i in range(yt_users):
                    local_ip  = avail_ips[i % len(avail_ips)]
                    worker_id = f"yt-{plan_id}-u{i}"

                    # Capture loop vars in closure
                    def _make_yt_cb(wid: str, lip: str):
                        def _cb(snap) -> None:
                            try:
                                d = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap)
                                d["session_id"]  = wid
                                d["stream_type"] = "youtube"
                                d["node_id"]     = self.config.node_id
                                asyncio.ensure_future(self.on_snapshot(d))
                            except Exception as exc:
                                logger.error("YouTube snapshot callback error: %s", exc)
                        return _cb

                    worker = YoutubeWorker(
                        worker_id   = worker_id,
                        local_ip    = local_ip,
                        url         = effective_url,
                        duration_s  = float(duration_s) if duration_s else None,
                        on_snapshot = _make_yt_cb(worker_id, local_ip),
                    )
                    self._tasks.append(
                        asyncio.create_task(worker.run(), name=f"yt-{worker_id}")
                    )
                logger.info(
                    "Started %d YouTube worker(s) at quality=720p → %s",
                    yt_users, effective_url,
                )
            except ImportError:
                logger.error(
                    "YoutubeWorker not available — ensure agent/workers/youtube_worker.py "
                    "is present and yt-dlp is installed (pip install yt-dlp)"
                )

        # 5. Duration watchdog
        if duration_s:
            async def _watchdog():
                await asyncio.sleep(float(duration_s))
                logger.info("Plan %s duration elapsed — stopping", plan_id)
                self._stop_event.set()
            self._tasks.append(asyncio.create_task(_watchdog(), name="watchdog"))

        # 6. Wait for stop
        await self._stop_event.wait()
        await self._teardown()
        logger.info("PlanExecutor: plan %s finished", plan_id)

    def stop(self) -> None:
        self._stop_event.set()

    def _wrap_snapshot(self, session_id: str, stream_type):
        """Returns a callback that enriches and forwards a snapshot."""
        def _cb(snap):
            try:
                d = snap.to_dict() if hasattr(snap, "to_dict") else snap
                d["session_id"]  = session_id
                d["stream_type"] = stream_type.value if hasattr(stream_type, "value") else str(stream_type)
                asyncio.ensure_future(self.on_snapshot(d))
            except Exception as exc:
                logger.error("Snapshot wrap error: %s", exc)
        return _cb

    async def _teardown(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._ip_mgr:
            await self._ip_mgr.teardown_all()
        logger.info("PlanExecutor: teardown complete")


# ---------------------------------------------------------------------------
# Agent main loop
# ---------------------------------------------------------------------------

class Agent:
    """
    Main agent process.  Manages the WS connection to the controller
    and delegates plan execution to PlanExecutor.
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config   = config
        self._executor: PlanExecutor | None = None
        self._ws       = None
        self._running  = True

    async def _send(self, msg_type: str, payload: dict) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": msg_type, "payload": payload}))
            except Exception as exc:
                logger.warning("Send failed: %s", exc)

    async def _on_snapshot(self, data: dict) -> None:
        await self._send("SNAPSHOT", data)

    async def run(self) -> None:
        """Main reconnect loop."""
        backoff = 2.0
        while self._running:
            try:
                await self._connect_and_loop()
                backoff = 2.0  # reset on clean disconnect
            except Exception as exc:
                logger.error("Connection error: %s — retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)

    async def _measure_clock_delta(self) -> float | None:
        """
        HTTP GET /api/health on the controller, measure clock synchronisation
        using a simplified NTP algorithm.

        Algorithm:
            t0          = agent clock before request
            server_time = controller's time.time() from the JSON response
            t1          = agent clock after response received
            offset      = (t0 + t1) / 2  −  server_time
                          (positive → agent is ahead; negative → agent is behind)

        Returns absolute offset in milliseconds, or None if the request fails.
        """
        import json as _json
        import urllib.request
        import urllib.error

        # Derive HTTP base URL from the WebSocket URL
        ws_url   = self.config.controller_url          # e.g. ws://192.168.1.10:8000/ws/agent
        http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
        base_url = http_url.rsplit("/ws/", 1)[0]       # e.g. http://192.168.1.10:8000
        health_url = f"{base_url}/api/health"

        try:
            t0 = time.time()
            req = urllib.request.urlopen(health_url, timeout=5)   # type: ignore[attr-defined]
            body = _json.loads(req.read())
            t1 = time.time()

            server_time = body.get("server_time")
            if server_time is None:
                # Older controller without server_time — use half-RTT as a proxy
                delta_ms = (t1 - t0) / 2.0 * 1000.0
                logger.debug("Clock delta (RTT/2 estimate): %.1f ms", delta_ms)
                return delta_ms

            agent_midpoint = (t0 + t1) / 2.0
            delta_ms = abs(agent_midpoint - server_time) * 1000.0
            logger.debug(
                "Clock delta: %.1f ms  (RTT=%.1f ms)", delta_ms, (t1 - t0) * 1000.0
            )
            if delta_ms > 50.0:
                logger.warning(
                    "Clock delta %.1f ms exceeds 50 ms threshold — one-way latency "
                    "readings may be inaccurate. Ensure NTP is running on all VMs.",
                    delta_ms,
                )
            return delta_ms

        except Exception as exc:
            logger.warning("Clock delta check failed (%s) — proceeding without sync info", exc)
            return None

    async def _connect_and_loop(self) -> None:
        if not _HAS_WEBSOCKETS:
            logger.error("websockets library required. Install with: pip install websockets")
            await asyncio.sleep(5)
            return

        # Measure clock delta before opening the WebSocket
        clock_delta_ms = await self._measure_clock_delta()

        logger.info("Connecting to controller: %s", self.config.controller_url)
        async with websockets.connect(
            self.config.controller_url,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            logger.info("Connected.")

            # Register — include clock_delta_ms so the controller can surface it
            register_payload: dict = {
                "node_id":        self.config.node_id,
                "hostname":       self.config.hostname,
                "ip_range_start": self.config.ip_start,
                "ip_range_end":   self.config.ip_end,
                "max_users":      self.config.max_users,
                "agent_version":  "1.0.0",
            }
            if clock_delta_ms is not None:
                register_payload["clock_delta_ms"] = round(clock_delta_ms, 2)
            await self._send("REGISTER", register_payload)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                payload  = msg.get("payload", {})

                if msg_type == "PING":
                    await self._send("PONG", {})

                elif msg_type == "RUN_PLAN":
                    if self._executor:
                        logger.warning("Already running a plan — ignoring RUN_PLAN")
                        continue
                    plan_id = payload.get("plan_id", "unknown")
                    await self._send("PLAN_STARTED", {"plan_id": plan_id})
                    self._executor = PlanExecutor(self.config, self._on_snapshot)
                    asyncio.create_task(self._run_plan_task(payload))

                elif msg_type == "STOP_PLAN":
                    if self._executor:
                        self._executor.stop()
                        self._executor = None
                        plan_id = payload.get("plan_id", "unknown")
                        await self._send("PLAN_STOPPED", {"plan_id": plan_id})

        self._ws = None

    async def _run_plan_task(self, node_plan: dict) -> None:
        try:
            await self._executor.run(node_plan)
        except Exception as exc:
            logger.error("Plan execution error: %s", exc)
            await self._send("ERROR", {"message": str(exc)})
        finally:
            plan_id = node_plan.get("plan_id", "unknown")
            await self._send("PLAN_STOPPED", {"plan_id": plan_id})
            self._executor = None

    def shutdown(self) -> None:
        self._running = False
        if self._executor:
            self._executor.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NetLab worker agent")
    parser.add_argument("--controller",  default="ws://127.0.0.1:8000/ws/agent",
                        help="Controller WebSocket URL")
    parser.add_argument("--node-id",     required=True, help="Unique node identifier")
    parser.add_argument("--ip-start",    required=True, help="First IP alias (e.g. 192.168.1.101)")
    parser.add_argument("--ip-end",      required=True, help="Last  IP alias (e.g. 192.168.1.120)")
    parser.add_argument("--interface",   default="eth0",  help="Network interface name")
    parser.add_argument("--max-users",   type=int, default=20)
    parser.add_argument("--log-level",   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    config = AgentConfig(
        controller_url = args.controller,
        node_id        = args.node_id,
        ip_start       = args.ip_start,
        ip_end         = args.ip_end,
        interface      = args.interface,
        max_users      = args.max_users,
    )

    agent = Agent(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, agent.shutdown)

    try:
        loop.run_until_complete(agent.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
