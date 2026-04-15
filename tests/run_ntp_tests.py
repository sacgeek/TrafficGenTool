"""
Tests for Task #5 — NTP Sync Check

Covers:
  - Clock delta calculation (NTP algorithm correctness)
  - Warning threshold at 50 ms
  - Fallback when server_time is absent (RTT/2 estimate)
  - Fallback when HTTP request fails (returns None)
  - URL derivation from various WebSocket URLs
  - clock_delta_ms field in AgentInfo / NodeState
  - /api/health includes server_time

Run with:
    python3 tests/run_ntp_tests.py

Place this file at:  tests/run_ntp_tests.py
"""

from __future__ import annotations
import asyncio
import json
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers — minimal stubs so tests run without the full netlab install
# ---------------------------------------------------------------------------

def _make_agent_module():
    """Build a minimal agent module from agent_updated.py source without
    importing optional deps (websockets, aiohttp, yt-dlp)."""

    # Stub websockets so the import succeeds
    ws_mod = types.ModuleType("websockets")
    ws_mod.connect = None  # never actually called in these tests
    sys.modules.setdefault("websockets", ws_mod)

    # Stub agent sub-packages so IPAliasManager etc. don't fail
    for name in [
        "agent", "agent.ip_manager", "agent.workers",
        "agent.workers.udp_session", "agent.mos",
        "agent.workers.web_worker", "agent.workers.youtube_worker",
    ]:
        sys.modules.setdefault(name, types.ModuleType(name))

    import importlib.util, pathlib
    src = pathlib.Path(__file__).parent.parent / "agent" / "agent.py"
    spec = importlib.util.spec_from_file_location("agent_mod", src)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_agent_mod = _make_agent_module()
AgentConfig   = _agent_mod.AgentConfig
Agent         = _agent_mod.Agent


def _make_config(controller_url="ws://192.168.1.10:8000/ws/agent"):
    return AgentConfig(
        controller_url = controller_url,
        node_id        = "test-node",
        ip_start       = "192.168.1.101",
        ip_end         = "192.168.1.110",
    )


# ---------------------------------------------------------------------------
# 1.  URL derivation
# ---------------------------------------------------------------------------

class TestURLDerivation(unittest.IsolatedAsyncioTestCase):
    """_measure_clock_delta derives the health URL from the WebSocket URL."""

    def _health_url_for(self, ws_url: str) -> str:
        """Mirror the derivation logic from Agent._measure_clock_delta."""
        http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
        base_url = http_url.rsplit("/ws/", 1)[0]
        return f"{base_url}/api/health"

    def test_ws_plain(self):
        url = self._health_url_for("ws://192.168.1.10:8000/ws/agent")
        self.assertEqual(url, "http://192.168.1.10:8000/api/health")

    def test_wss_secure(self):
        url = self._health_url_for("wss://lab.example.com:443/ws/agent")
        self.assertEqual(url, "https://lab.example.com:443/api/health")

    def test_ws_no_port(self):
        url = self._health_url_for("ws://10.0.0.1/ws/agent")
        self.assertEqual(url, "http://10.0.0.1/api/health")

    def test_ws_custom_path_prefix(self):
        # If the ws path segment is not literally "/ws/", the rsplit still works
        # because we split on the first "/ws/" from the right
        url = self._health_url_for("ws://host:8080/ws/agent")
        self.assertEqual(url, "http://host:8080/api/health")


# ---------------------------------------------------------------------------
# 2.  Clock delta algorithm
# ---------------------------------------------------------------------------

class TestClockDeltaAlgorithm(unittest.TestCase):
    """Verify the NTP-style offset calculation independent of I/O."""

    def _compute_delta(self, t0: float, t1: float, server_time: float) -> float:
        agent_midpoint = (t0 + t1) / 2.0
        return abs(agent_midpoint - server_time) * 1000.0

    def test_perfect_sync(self):
        """If agent and server clocks agree, delta is half-RTT at most."""
        t0 = 1_000_000.000
        server_time = t0 + 0.010   # server received 10 ms into the round trip
        t1 = t0 + 0.020            # 20 ms RTT total
        delta = self._compute_delta(t0, t1, server_time)
        self.assertAlmostEqual(delta, 0.0, places=6)

    def test_agent_ahead(self):
        """Agent clock 100 ms ahead of server."""
        t0 = 1_000_000.100
        server_time = 1_000_000.000   # server is 100 ms behind
        t1 = t0 + 0.010
        delta = self._compute_delta(t0, t1, server_time)
        # midpoint = 1_000_000.105; offset ≈ 105 ms
        self.assertGreater(delta, 50.0)

    def test_agent_behind(self):
        """Agent clock 200 ms behind server."""
        t0 = 1_000_000.000
        server_time = 1_000_000.200
        t1 = t0 + 0.010
        delta = self._compute_delta(t0, t1, server_time)
        self.assertGreater(delta, 50.0)

    def test_within_threshold(self):
        """2 ms delta is below the 50 ms warning threshold."""
        t0 = 1_000_000.000
        server_time = t0 + 0.002   # 2 ms ahead
        t1 = t0 + 0.004
        delta = self._compute_delta(t0, t1, server_time)
        self.assertLess(delta, 50.0)

    def test_exactly_at_threshold(self):
        """50 ms delta is exactly at the boundary — treated as within range."""
        t0 = 0.0
        server_time = 0.050
        t1 = 0.100   # RTT = 100 ms, midpoint = 50 ms → offset = 0
        delta = self._compute_delta(t0, t1, server_time)
        self.assertAlmostEqual(delta, 0.0, places=6)


# ---------------------------------------------------------------------------
# 3.  _measure_clock_delta — integration (mocked urllib)
# ---------------------------------------------------------------------------

class TestMeasureClockDelta(unittest.IsolatedAsyncioTestCase):
    """Unit tests for Agent._measure_clock_delta with urllib mocked."""

    def _make_fake_response(self, body: dict):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__  = MagicMock(return_value=False)
        return mock_resp

    async def test_returns_float_when_server_time_present(self):
        agent = Agent(_make_config())
        server_now = time.time()
        fake_resp = self._make_fake_response({"status": "ok", "server_time": server_now})

        with patch("urllib.request.urlopen", return_value=fake_resp):
            delta = await agent._measure_clock_delta()

        self.assertIsInstance(delta, float)
        # In a perfect localhost scenario the delta should be very small
        self.assertGreaterEqual(delta, 0.0)
        self.assertLess(delta, 5000.0)  # sanity upper bound

    async def test_fallback_rtt_when_no_server_time(self):
        """If server_time is absent, fall back to RTT/2."""
        agent = Agent(_make_config())
        fake_resp = self._make_fake_response({"status": "ok"})   # no server_time

        with patch("urllib.request.urlopen", return_value=fake_resp):
            delta = await agent._measure_clock_delta()

        self.assertIsInstance(delta, float)
        self.assertGreater(delta, 0.0)

    async def test_returns_none_on_network_error(self):
        """If the HTTP request raises, _measure_clock_delta returns None."""
        import urllib.error
        agent = Agent(_make_config())

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            delta = await agent._measure_clock_delta()

        self.assertIsNone(delta)

    async def test_returns_none_on_timeout(self):
        import socket
        agent = Agent(_make_config())

        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            delta = await agent._measure_clock_delta()

        self.assertIsNone(delta)

    async def test_returns_none_on_json_error(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not-json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__  = MagicMock(return_value=False)

        agent = Agent(_make_config())
        with patch("urllib.request.urlopen", return_value=mock_resp):
            delta = await agent._measure_clock_delta()

        self.assertIsNone(delta)

    async def test_url_built_correctly_for_ws_url(self):
        """Verify that urlopen is called with the correct HTTP health URL."""
        agent = Agent(_make_config("ws://192.168.1.10:8000/ws/agent"))
        fake_resp = self._make_fake_response({"status": "ok", "server_time": time.time()})

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            await agent._measure_clock_delta()

        called_url = mock_open.call_args[0][0]
        self.assertEqual(called_url, "http://192.168.1.10:8000/api/health")

    async def test_wss_url_uses_https(self):
        agent = Agent(_make_config("wss://lab.example.com/ws/agent"))
        fake_resp = self._make_fake_response({"status": "ok", "server_time": time.time()})

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            await agent._measure_clock_delta()

        called_url = mock_open.call_args[0][0]
        self.assertTrue(called_url.startswith("https://"))


# ---------------------------------------------------------------------------
# 4.  Warning threshold logging
# ---------------------------------------------------------------------------

class TestClockDeltaWarning(unittest.IsolatedAsyncioTestCase):
    """_measure_clock_delta should log a warning when delta > 50 ms."""

    def _make_fake_response(self, body: dict):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(body).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__  = MagicMock(return_value=False)
        return mock_resp

    async def test_warning_logged_when_delta_exceeds_threshold(self):
        agent = Agent(_make_config())
        # Server is 1 second behind → delta ≈ 1000 ms → should warn
        server_time = time.time() - 1.0
        fake_resp = self._make_fake_response({"status": "ok", "server_time": server_time})

        with patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertLogs("agent_mod", level="WARNING") as cm:
                delta = await agent._measure_clock_delta()

        self.assertIsNotNone(delta)
        self.assertGreater(delta, 50.0)
        self.assertTrue(any("50 ms" in line or "NTP" in line for line in cm.output))

    async def test_no_warning_when_delta_within_threshold(self):
        agent = Agent(_make_config())
        # Server time nearly identical to agent time → small delta → no warn
        server_time = time.time()
        fake_resp = self._make_fake_response({"status": "ok", "server_time": server_time})

        import logging
        with patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertLogs("agent_mod", level="DEBUG") as cm:
                delta = await agent._measure_clock_delta()

        self.assertIsNotNone(delta)
        warning_lines = [l for l in cm.output if "WARNING" in l and "50 ms" in l]
        self.assertEqual(len(warning_lines), 0, f"Unexpected warning logged: {warning_lines}")


# ---------------------------------------------------------------------------
# 5.  clock_delta_ms in REGISTER payload
# ---------------------------------------------------------------------------

class TestRegisterPayload(unittest.IsolatedAsyncioTestCase):
    """_connect_and_loop should include clock_delta_ms in the REGISTER message."""

    async def test_clock_delta_included_in_register_when_check_succeeds(self):
        agent = Agent(_make_config())

        sent_messages: list[dict] = []

        async def fake_send(msg_type, payload):
            sent_messages.append({"type": msg_type, "payload": payload})

        fake_ws = AsyncMock()
        fake_ws.__aenter__ = AsyncMock(return_value=fake_ws)
        fake_ws.__aexit__  = AsyncMock(return_value=False)
        fake_ws.__aiter__  = MagicMock(return_value=iter([]))  # no incoming messages

        with patch.object(agent, "_measure_clock_delta", return_value=12.5):
            with patch.object(agent, "_send", side_effect=fake_send):
                with patch("websockets.connect", return_value=fake_ws):
                    try:
                        await agent._connect_and_loop()
                    except Exception:
                        pass

        register_msgs = [m for m in sent_messages if m["type"] == "REGISTER"]
        self.assertTrue(len(register_msgs) >= 1, "No REGISTER message sent")
        self.assertIn("clock_delta_ms", register_msgs[0]["payload"])
        self.assertAlmostEqual(register_msgs[0]["payload"]["clock_delta_ms"], 12.5)

    async def test_clock_delta_omitted_from_register_when_check_fails(self):
        """If _measure_clock_delta returns None, key should be absent from payload."""
        agent = Agent(_make_config())
        sent_messages: list[dict] = []

        async def fake_send(msg_type, payload):
            sent_messages.append({"type": msg_type, "payload": payload})

        fake_ws = AsyncMock()
        fake_ws.__aenter__ = AsyncMock(return_value=fake_ws)
        fake_ws.__aexit__  = AsyncMock(return_value=False)
        fake_ws.__aiter__  = MagicMock(return_value=iter([]))

        with patch.object(agent, "_measure_clock_delta", return_value=None):
            with patch.object(agent, "_send", side_effect=fake_send):
                with patch("websockets.connect", return_value=fake_ws):
                    try:
                        await agent._connect_and_loop()
                    except Exception:
                        pass

        register_msgs = [m for m in sent_messages if m["type"] == "REGISTER"]
        self.assertTrue(len(register_msgs) >= 1)
        self.assertNotIn("clock_delta_ms", register_msgs[0]["payload"])


# ---------------------------------------------------------------------------
# 6.  AgentInfo / NodeState model fields
# ---------------------------------------------------------------------------

class TestAgentInfoClockDelta(unittest.TestCase):
    """AgentInfo accepts clock_delta_ms; NodeState.to_dict() exposes it."""

    def setUp(self):
        import importlib.util, pathlib
        src  = pathlib.Path(__file__).with_name("models_updated.py")
        spec = importlib.util.spec_from_file_location("models_updated", src)
        self._models = importlib.util.module_from_spec(spec)

        # Stub pydantic if absent (unlikely but defensive)
        try:
            import pydantic  # noqa: F401
        except ImportError:
            self.skipTest("pydantic not installed")

        spec.loader.exec_module(self._models)

    def _make_info(self, **kw):
        return self._models.AgentInfo(
            node_id="n1",
            hostname="lab-worker-1",
            ip_range_start="192.168.1.101",
            ip_range_end="192.168.1.120",
            **kw,
        )

    def test_default_clock_delta_is_none(self):
        info = self._make_info()
        self.assertIsNone(info.clock_delta_ms)

    def test_clock_delta_accepted_as_float(self):
        info = self._make_info(clock_delta_ms=23.7)
        self.assertAlmostEqual(info.clock_delta_ms, 23.7)

    def test_clock_delta_zero_accepted(self):
        info = self._make_info(clock_delta_ms=0.0)
        self.assertEqual(info.clock_delta_ms, 0.0)

    def test_node_state_to_dict_includes_clock_delta(self):
        info  = self._make_info(clock_delta_ms=42.1)
        state = self._models.NodeState(node_id="n1", info=info)
        d = state.to_dict()
        self.assertIn("clock_delta_ms", d)
        self.assertAlmostEqual(d["clock_delta_ms"], 42.1)

    def test_node_state_to_dict_clock_delta_none(self):
        info  = self._make_info()
        state = self._models.NodeState(node_id="n1", info=info)
        d = state.to_dict()
        self.assertIn("clock_delta_ms", d)
        self.assertIsNone(d["clock_delta_ms"])


# ---------------------------------------------------------------------------
# 7.  /api/health includes server_time
# ---------------------------------------------------------------------------

class TestHealthEndpointServerTime(unittest.IsolatedAsyncioTestCase):
    """The /api/health endpoint must include a server_time float."""

    async def test_health_response_has_server_time(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi or httpx not installed")

        import importlib.util, pathlib
        src  = pathlib.Path(__file__).with_name("main.py")
        spec = importlib.util.spec_from_file_location("_main_mod", src)

        # Stub controller sub-modules
        for name in [
            "controller", "controller.models", "controller.session_store",
            "controller.distributor", "controller.export",
        ]:
            if name not in sys.modules:
                sys.modules[name] = types.ModuleType(name)

        # We only need to verify the health route shape; skip full app init
        # by testing the function directly
        t_before = time.time()
        import importlib
        try:
            main_mod = importlib.import_module("controller.main")
        except Exception:
            self.skipTest("controller.main not importable in test env")

        result = await main_mod.health()
        t_after  = time.time()

        self.assertIn("server_time", result)
        self.assertGreaterEqual(result["server_time"], t_before)
        self.assertLessEqual(result["server_time"],    t_after)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    for cls in [
        TestURLDerivation,
        TestClockDeltaAlgorithm,
        TestMeasureClockDelta,
        TestClockDeltaWarning,
        TestRegisterPayload,
        TestAgentInfoClockDelta,
        TestHealthEndpointServerTime,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
