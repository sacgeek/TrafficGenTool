"""
NetLab RADIUS worker — agent-side RADIUS client.

Executed by PlanExecutor when radius_enabled=True in the NodePlan:

  1. authenticate_all() — before traffic workers start.
     For each RadiusUser assigned to this node:
       a. Send Access-Request from the user's alias IP  (UDP → RADIUS server port 1812)
       b. Receive Access-Accept
       c. Send Accounting-Start                         (UDP → RADIUS server port 1813)
       d. Receive Accounting-Response

  2. logoff_all() — after traffic workers stop, before IP aliases are torn down.
     For each authenticated user:
       a. Send Accounting-Stop  (UDP → RADIUS server port 1813)
       b. Receive Accounting-Response

Packets are ClearPass-compatible:
  - PAP password (RFC 2865 §5.2 obfuscation) with a fixed dummy password
  - Framed-IP-Address included in both Access-Request and Accounting packets
  - Calling-Station-Id set to a deterministic MAC derived from the alias IP
  - NAS-Port-Type, NAS-Identifier forwarded from NodePlan config
  - Aruba User-Role VSA (vendor 14823, attr 1 = Aruba-User-Role) is carried in the Accept
    response from the server; the agent does not need to set it

All network operations are asyncio-native (create_datagram_endpoint) and
use per-user bound sockets so each packet actually leaves from the correct
alias IP.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RADIUS constants
# ---------------------------------------------------------------------------

CODE_ACCESS_REQUEST      = 1
CODE_ACCESS_ACCEPT       = 2
CODE_ACCESS_REJECT       = 3
CODE_ACCOUNTING_REQUEST  = 4
CODE_ACCOUNTING_RESPONSE = 5

ATTR_USER_NAME           = 1
ATTR_USER_PASSWORD       = 2
ATTR_NAS_IP_ADDRESS      = 4
ATTR_NAS_PORT            = 5
ATTR_SERVICE_TYPE        = 6
ATTR_FRAMED_IP_ADDRESS   = 8
ATTR_CALLED_STATION_ID   = 30
ATTR_CALLING_STATION_ID  = 31
ATTR_NAS_IDENTIFIER      = 32
ATTR_ACCT_STATUS_TYPE    = 40
ATTR_ACCT_INPUT_OCTETS   = 42
ATTR_ACCT_OUTPUT_OCTETS  = 43
ATTR_ACCT_SESSION_ID     = 44
ATTR_ACCT_SESSION_TIME   = 46
ATTR_NAS_PORT_TYPE       = 61

SERVICE_TYPE_FRAMED      = 2
ACCT_STATUS_START        = 1
ACCT_STATUS_STOP         = 2

_DUMMY_PASSWORD          = "netlab-sim"
_CALLED_STATION_ID       = "netlab-nas"

# ---------------------------------------------------------------------------
# Packet utilities
# ---------------------------------------------------------------------------

def _tlv(attr_type: int, value: bytes) -> bytes:
    return struct.pack("BB", attr_type, len(value) + 2) + value

def _str_attr(t: int, s: str) -> bytes:
    return _tlv(t, s.encode("utf-8"))

def _int_attr(t: int, n: int) -> bytes:
    return _tlv(t, struct.pack(">I", n))

def _ip_attr(t: int, ip: str) -> bytes:
    parts = list(map(int, ip.split(".")))
    return _tlv(t, struct.pack("BBBB", *parts))

def _encrypt_password(password: str, secret: str, authenticator: bytes) -> bytes:
    """RFC 2865 §5.2 PAP password obfuscation."""
    pw = password.encode("utf-8")
    if len(pw) % 16:
        pw += b"\x00" * (16 - len(pw) % 16)
    result = b""
    last = authenticator
    for i in range(0, len(pw), 16):
        digest = hashlib.md5(secret.encode("utf-8") + last).digest()
        chunk  = bytes(a ^ b for a, b in zip(pw[i : i + 16], digest))
        result += chunk
        last = chunk
    return result

def _build_packet(code: int, identifier: int, authenticator: bytes, attrs: bytes) -> bytes:
    length = 20 + len(attrs)
    return struct.pack(">BBH16s", code, identifier, length, authenticator) + attrs

def _accounting_authenticator(
    code: int, identifier: int, attrs: bytes, secret: str
) -> bytes:
    """RFC 2866 §3 — accounting request authenticator (zeros in place of auth field)."""
    length = 20 + len(attrs)
    data = (
        struct.pack(">BBH", code, identifier, length)
        + b"\x00" * 16
        + attrs
        + secret.encode("utf-8")
    )
    return hashlib.md5(data).digest()

def _get_own_ip() -> str:
    """Return the agent's primary outbound IP (used as NAS-IP-Address)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------------------
# One-shot UDP send/receive (asyncio DatagramProtocol)
# ---------------------------------------------------------------------------

class _OneShotProtocol(asyncio.DatagramProtocol):
    """Send one datagram and resolve a future with the first reply."""

    def __init__(self, request: bytes, future: asyncio.Future) -> None:
        self._request = request
        self._future  = future
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport
        transport.sendto(self._request)  # type: ignore[attr-defined]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self._future.done():
            self._future.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._future.done():
            self._future.set_exception(
                exc or ConnectionError("RADIUS connection lost before response")
            )


async def _send_recv(
    local_ip:    str,
    server_ip:   str,
    server_port: int,
    request:     bytes,
    timeout:     float = 5.0,
) -> bytes:
    """
    Send *request* as a UDP datagram bound to *local_ip* and return
    the first response datagram received, within *timeout* seconds.

    Raises asyncio.TimeoutError if no response arrives in time.
    """
    loop   = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _OneShotProtocol(request, future),
        local_addr  = (local_ip, 0),
        remote_addr = (server_ip, server_port),
    )
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    finally:
        transport.close()

# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------

class _UserState:
    __slots__ = (
        "username", "ip_address", "mac_address", "aruba_role",
        "acct_session_id", "authenticated", "auth_start_time",
    )

    def __init__(self, user: dict) -> None:
        self.username        = user["username"]
        self.ip_address      = user["ip_address"]
        self.mac_address     = user["mac_address"]
        self.aruba_role      = user["aruba_role"]
        self.acct_session_id = user["acct_session_id"]
        self.authenticated   = False
        self.auth_start_time: float | None = None


# ---------------------------------------------------------------------------
# RadiusWorker
# ---------------------------------------------------------------------------

class RadiusWorker:
    """
    Manages RADIUS auth and accounting for all simulated users on one agent node.

    Usage in PlanExecutor:
        worker = RadiusWorker(node_plan)
        await worker.authenticate_all()   # before traffic workers
        # ... traffic runs ...
        await worker.logoff_all()         # in _teardown(), before alias removal
    """

    def __init__(self, node_plan: dict) -> None:
        self._server_ip    = node_plan.get("radius_server_ip", "127.0.0.1")
        self._secret       = node_plan.get("radius_secret", "testing123")
        self._nas_id       = node_plan.get("nas_identifier", "netlab-controller")
        self._nas_port_type = node_plan.get("nas_port_type", 15)
        self._nas_ip       = _get_own_ip()
        self._users        = [
            _UserState(u) for u in node_plan.get("radius_users", [])
        ]
        self._auth_port    = 1812
        self._acct_port    = 1813

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def authenticate_all(self) -> None:
        """
        Send Access-Request + Accounting-Start for every user concurrently.
        Failures are logged and skipped; they don't block the rest.
        """
        if not self._users:
            return
        logger.info(
            "RADIUS: authenticating %d users → %s", len(self._users), self._server_ip
        )
        results = await asyncio.gather(
            *[self._auth_user(u) for u in self._users],
            return_exceptions=True,
        )
        ok  = sum(1 for r in results if r is not True and not isinstance(r, Exception))
        ok  = sum(1 for u in self._users if u.authenticated)
        fail = len(self._users) - ok
        logger.info("RADIUS: %d authenticated, %d failed", ok, fail)

    async def logoff_all(self) -> None:
        """
        Send Accounting-Stop for every successfully authenticated user.
        """
        authed = [u for u in self._users if u.authenticated]
        if not authed:
            return
        logger.info("RADIUS: sending Accounting-Stop for %d users", len(authed))
        await asyncio.gather(
            *[self._acct_stop(u) for u in authed],
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Per-user auth flow
    # ------------------------------------------------------------------

    async def _auth_user(self, user: _UserState) -> None:
        identifier = int.from_bytes(os.urandom(1), "big")
        try:
            # 1. Access-Request
            request = self._build_access_request(user, identifier)
            response = await _send_recv(
                user.ip_address, self._server_ip, self._auth_port, request
            )
            code = response[0] if response else 0
            if code != CODE_ACCESS_ACCEPT:
                logger.warning(
                    "RADIUS: Access-Reject/error for %s (code=%d)", user.username, code
                )
                return

            user.authenticated   = True
            user.auth_start_time = time.time()
            logger.debug("RADIUS: Access-Accept for %s @ %s", user.username, user.ip_address)

            # 2. Accounting-Start
            acct_req = self._build_accounting_request(user, ACCT_STATUS_START)
            await _send_recv(
                user.ip_address, self._server_ip, self._acct_port, acct_req
            )
            logger.debug("RADIUS: Accounting-Start sent for %s", user.username)

        except asyncio.TimeoutError:
            logger.warning(
                "RADIUS: timeout for %s @ %s (server=%s)",
                user.username, user.ip_address, self._server_ip,
            )
        except Exception as exc:
            logger.error("RADIUS: auth error for %s: %s", user.username, exc)

    async def _acct_stop(self, user: _UserState) -> None:
        session_time = int(time.time() - user.auth_start_time) if user.auth_start_time else 0
        try:
            req = self._build_accounting_request(
                user, ACCT_STATUS_STOP, session_time=session_time
            )
            await _send_recv(
                user.ip_address, self._server_ip, self._acct_port, req
            )
            logger.debug(
                "RADIUS: Accounting-Stop sent for %s (session_time=%ds)",
                user.username, session_time,
            )
        except asyncio.TimeoutError:
            logger.warning("RADIUS: Accounting-Stop timeout for %s", user.username)
        except Exception as exc:
            logger.error("RADIUS: Accounting-Stop error for %s: %s", user.username, exc)

    # ------------------------------------------------------------------
    # Packet builders
    # ------------------------------------------------------------------

    def _build_access_request(self, user: _UserState, identifier: int) -> bytes:
        """Build a ClearPass-compatible Access-Request packet."""
        request_auth = os.urandom(16)

        attrs = b""
        attrs += _str_attr(ATTR_USER_NAME,          user.username)
        attrs += _tlv(ATTR_USER_PASSWORD,
                      _encrypt_password(_DUMMY_PASSWORD, self._secret, request_auth))
        attrs += _ip_attr(ATTR_NAS_IP_ADDRESS,      self._nas_ip)
        attrs += _int_attr(ATTR_NAS_PORT,           self._users.index(user))
        attrs += _int_attr(ATTR_SERVICE_TYPE,       SERVICE_TYPE_FRAMED)
        attrs += _ip_attr(ATTR_FRAMED_IP_ADDRESS,   user.ip_address)
        attrs += _str_attr(ATTR_NAS_IDENTIFIER,     self._nas_id)
        attrs += _str_attr(ATTR_CALLING_STATION_ID, user.mac_address)
        attrs += _str_attr(ATTR_CALLED_STATION_ID,  _CALLED_STATION_ID)
        attrs += _int_attr(ATTR_NAS_PORT_TYPE,      self._nas_port_type)

        return _build_packet(CODE_ACCESS_REQUEST, identifier, request_auth, attrs)

    def _build_accounting_request(
        self,
        user:         _UserState,
        status:       int,
        session_time: int = 0,
    ) -> bytes:
        """
        Build a ClearPass-compatible Accounting-Request packet.

        status:       ACCT_STATUS_START (1) or ACCT_STATUS_STOP (2)
        session_time: elapsed seconds — only meaningful for Stop
        """
        identifier = int.from_bytes(os.urandom(1), "big")

        attrs = b""
        attrs += _int_attr(ATTR_ACCT_STATUS_TYPE,   status)
        attrs += _str_attr(ATTR_ACCT_SESSION_ID,    user.acct_session_id)
        attrs += _str_attr(ATTR_USER_NAME,          user.username)
        attrs += _ip_attr(ATTR_NAS_IP_ADDRESS,      self._nas_ip)
        attrs += _ip_attr(ATTR_FRAMED_IP_ADDRESS,   user.ip_address)
        attrs += _str_attr(ATTR_NAS_IDENTIFIER,     self._nas_id)
        attrs += _str_attr(ATTR_CALLING_STATION_ID, user.mac_address)
        attrs += _int_attr(ATTR_NAS_PORT_TYPE,      self._nas_port_type)

        if status == ACCT_STATUS_STOP:
            attrs += _int_attr(ATTR_ACCT_SESSION_TIME,  session_time)
            attrs += _int_attr(ATTR_ACCT_INPUT_OCTETS,  0)
            attrs += _int_attr(ATTR_ACCT_OUTPUT_OCTETS, 0)

        # Accounting authenticator uses zeroed auth field (RFC 2866 §3)
        auth = _accounting_authenticator(
            CODE_ACCOUNTING_REQUEST, identifier, attrs, self._secret
        )
        return _build_packet(CODE_ACCOUNTING_REQUEST, identifier, auth, attrs)
