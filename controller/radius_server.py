"""
NetLab RADIUS server — asyncio UDP listener.

Listens on UDP 1812 (authentication) and UDP 1813 (accounting).
Only started when the plan's radius_server_ip matches the controller's own IP
(i.e. internal mode).  When an external RADIUS server (e.g. ClearPass) is
configured, this module is not activated and all RADIUS traffic flows directly
between agents and the external server.

Packet format follows RFC 2865 (authentication) and RFC 2866 (accounting).
Responses include Framed-IP-Address in Access-Accept so ClearPass and real
Aruba gear can correlate the user's assigned IP with their role.

Aruba User-Role VSA:
    Vendor-Id  : 14823
    Vendor-Type: 25  (Aruba-User-Role)
    Value      : UTF-8 role string, e.g. "Employee"
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RADIUS constants (RFC 2865 / 2866)
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
ATTR_VENDOR_SPECIFIC     = 26
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
ACCT_STATUS_INTERIM      = 3

ARUBA_VENDOR_ID          = 14823
ARUBA_USER_ROLE_ATTR     = 25


# ---------------------------------------------------------------------------
# Low-level packet utilities
# ---------------------------------------------------------------------------

def _tlv(attr_type: int, value: bytes) -> bytes:
    """Encode a single RADIUS TLV attribute."""
    return struct.pack("BB", attr_type, len(value) + 2) + value


def _str_attr(attr_type: int, value: str) -> bytes:
    return _tlv(attr_type, value.encode("utf-8"))


def _int_attr(attr_type: int, value: int) -> bytes:
    return _tlv(attr_type, struct.pack(">I", value))


def _ip_attr(attr_type: int, ip: str) -> bytes:
    parts = list(map(int, ip.split(".")))
    return _tlv(attr_type, struct.pack("BBBB", *parts))


def _vsa(vendor_id: int, vendor_type: int, value: bytes) -> bytes:
    """Encode a Vendor-Specific Attribute (RFC 2865 §5.26)."""
    vendor_bytes = struct.pack(">I", vendor_id)
    inner = vendor_bytes + struct.pack("BB", vendor_type, len(value) + 2) + value
    return _tlv(ATTR_VENDOR_SPECIFIC, inner)


def _build_packet(code: int, identifier: int, authenticator: bytes, attrs: bytes) -> bytes:
    length = 20 + len(attrs)
    return struct.pack(">BBH16s", code, identifier, length, authenticator) + attrs


def _response_authenticator(
    code: int, identifier: int, request_auth: bytes, attrs: bytes, secret: str
) -> bytes:
    """
    Compute the Response Authenticator per RFC 2865 §3.

        MD5(Code + ID + Length + RequestAuth + ResponseAttrs + Secret)
    """
    length = 20 + len(attrs)
    data = (
        struct.pack(">BBH", code, identifier, length)
        + request_auth
        + attrs
        + secret.encode("utf-8")
    )
    return hashlib.md5(data).digest()


def _parse_attrs(payload: bytes) -> list[tuple[int, bytes]]:
    """Parse the attribute section of a RADIUS packet into (type, value) pairs."""
    attrs: list[tuple[int, bytes]] = []
    pos = 0
    while pos + 1 < len(payload):
        atype = payload[pos]
        alen  = payload[pos + 1]
        if alen < 2 or pos + alen > len(payload):
            break
        attrs.append((atype, payload[pos + 2 : pos + alen]))
        pos += alen
    return attrs


def _get_attr(attrs: list[tuple[int, bytes]], atype: int) -> bytes | None:
    for t, v in attrs:
        if t == atype:
            return v
    return None


def _parse_ip(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def _parse_int(raw: bytes) -> int:
    return struct.unpack(">I", raw)[0] if len(raw) >= 4 else 0


# ---------------------------------------------------------------------------
# RADIUS server state
# ---------------------------------------------------------------------------

class RadiusServer:
    """
    In-memory RADIUS server for the NetLab controller.

    Thread-safe for asyncio (single-threaded event loop).
    State is updated at plan creation / completion via set_session() /
    clear_session().  Multiple RADIUS sessions are not expected to overlap,
    but if they do the latest set_session() call wins.
    """

    def __init__(self) -> None:
        self._secret:       str                    = ""
        # ip_address → RadiusUser dict (serialised as plain dict for speed)
        self._user_map:     dict[str, dict]        = {}
        self._auth_records: list[dict[str, Any]]   = []
        self._transport_auth: asyncio.BaseTransport | None = None
        self._transport_acct: asyncio.BaseTransport | None = None

    # ------------------------------------------------------------------
    # Session management (called by controller/main.py)
    # ------------------------------------------------------------------

    def set_session(self, secret: str, users: list[dict]) -> None:
        """
        Register the shared secret and user map for the current session.
        users is a list of RadiusUser.model_dump() dicts.
        """
        self._secret   = secret
        self._user_map = {u["ip_address"]: u for u in users}
        self._auth_records.clear()
        logger.info(
            "RADIUS session registered: %d users, secret=%s",
            len(users), "*" * len(secret),
        )

    def clear_session(self) -> None:
        self._user_map = {}
        logger.info("RADIUS session cleared")

    def get_auth_records(self) -> list[dict]:
        return list(self._auth_records)

    # ------------------------------------------------------------------
    # asyncio transport references (set by start())
    # ------------------------------------------------------------------

    def _set_transport_auth(self, transport: asyncio.BaseTransport) -> None:
        self._transport_auth = transport

    def _set_transport_acct(self, transport: asyncio.BaseTransport) -> None:
        self._transport_acct = transport

    # ------------------------------------------------------------------
    # Packet handlers
    # ------------------------------------------------------------------

    def handle_auth(self, data: bytes, addr: tuple) -> None:
        """Dispatch an incoming packet on the auth port (1812)."""
        if len(data) < 20:
            return
        code, identifier, _length = struct.unpack_from(">BBH", data, 0)
        request_auth = data[4:20]
        attrs = _parse_attrs(data[20:])

        if code == CODE_ACCESS_REQUEST:
            self._handle_access_request(data, addr, identifier, request_auth, attrs)
        else:
            logger.debug("Auth port: unexpected code %d from %s", code, addr)

    def handle_acct(self, data: bytes, addr: tuple) -> None:
        """Dispatch an incoming packet on the accounting port (1813)."""
        if len(data) < 20:
            return
        code, identifier, _length = struct.unpack_from(">BBH", data, 0)
        request_auth = data[4:20]
        attrs = _parse_attrs(data[20:])

        if code == CODE_ACCOUNTING_REQUEST:
            self._handle_accounting_request(data, addr, identifier, request_auth, attrs)
        else:
            logger.debug("Acct port: unexpected code %d from %s", code, addr)

    def _handle_access_request(
        self,
        _raw: bytes,
        addr:         tuple,
        identifier:   int,
        request_auth: bytes,
        attrs:        list[tuple[int, bytes]],
    ) -> None:
        if not self._transport_auth:
            return

        # Extract key attributes from the request
        username_raw   = _get_attr(attrs, ATTR_USER_NAME)
        framed_ip_raw  = _get_attr(attrs, ATTR_FRAMED_IP_ADDRESS)

        username  = username_raw.decode("utf-8")  if username_raw  else "unknown"
        framed_ip = _parse_ip(framed_ip_raw)       if framed_ip_raw else ""

        # Look up the pre-assigned role for this IP
        user_info = self._user_map.get(framed_ip, {})
        role      = user_info.get("aruba_role", "Employee")

        logger.info(
            "RADIUS Access-Request: user=%s ip=%s role=%s from=%s",
            username, framed_ip, role, addr,
        )

        # Build Access-Accept attributes
        resp_attrs = b""
        resp_attrs += _int_attr(ATTR_SERVICE_TYPE, SERVICE_TYPE_FRAMED)
        # Always include Framed-IP-Address in the Accept (ClearPass-compatible)
        if framed_ip:
            resp_attrs += _ip_attr(ATTR_FRAMED_IP_ADDRESS, framed_ip)
        # Aruba User-Role VSA (vendor 14823, attr 25)
        resp_attrs += _vsa(
            ARUBA_VENDOR_ID,
            ARUBA_USER_ROLE_ATTR,
            role.encode("utf-8"),
        )

        resp_auth = _response_authenticator(
            CODE_ACCESS_ACCEPT, identifier, request_auth, resp_attrs, self._secret
        )
        response = _build_packet(CODE_ACCESS_ACCEPT, identifier, resp_auth, resp_attrs)
        self._transport_auth.sendto(response, addr)  # type: ignore[attr-defined]

        # Record auth event for session history / API
        self._auth_records.append({
            "username":   username,
            "ip_address": framed_ip,
            "aruba_role": role,
            "auth_time":  time.time(),
            "acct_start_time": None,
            "acct_stop_time":  None,
            "acct_session_time": None,
            "nas_addr":   str(addr),
        })

    def _handle_accounting_request(
        self,
        _raw: bytes,
        addr:         tuple,
        identifier:   int,
        request_auth: bytes,
        attrs:        list[tuple[int, bytes]],
    ) -> None:
        if not self._transport_acct:
            return

        status_raw    = _get_attr(attrs, ATTR_ACCT_STATUS_TYPE)
        username_raw  = _get_attr(attrs, ATTR_USER_NAME)
        framed_raw    = _get_attr(attrs, ATTR_FRAMED_IP_ADDRESS)
        session_raw   = _get_attr(attrs, ATTR_ACCT_SESSION_ID)
        duration_raw  = _get_attr(attrs, ATTR_ACCT_SESSION_TIME)

        status     = _parse_int(status_raw)   if status_raw   else 0
        username   = username_raw.decode("utf-8") if username_raw else "unknown"
        framed_ip  = _parse_ip(framed_raw)    if framed_raw   else ""
        session_id = session_raw.decode("utf-8") if session_raw  else ""
        duration_s = _parse_int(duration_raw) if duration_raw  else None

        status_name = {
            ACCT_STATUS_START:   "Start",
            ACCT_STATUS_STOP:    "Stop",
            ACCT_STATUS_INTERIM: "Interim-Update",
        }.get(status, str(status))

        logger.info(
            "RADIUS Accounting-%s: user=%s ip=%s session=%s duration=%s",
            status_name, username, framed_ip, session_id,
            f"{duration_s}s" if duration_s is not None else "—",
        )

        # Update matching auth record
        now = time.time()
        for rec in self._auth_records:
            if rec.get("ip_address") == framed_ip:
                if status == ACCT_STATUS_START:
                    rec["acct_start_time"] = now
                elif status == ACCT_STATUS_STOP:
                    rec["acct_stop_time"]     = now
                    rec["acct_session_time"]  = duration_s
                break

        # Send Accounting-Response (no attributes required)
        resp_auth = _response_authenticator(
            CODE_ACCOUNTING_RESPONSE, identifier, request_auth, b"", self._secret
        )
        response = _build_packet(CODE_ACCOUNTING_RESPONSE, identifier, resp_auth, b"")
        self._transport_acct.sendto(response, addr)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# asyncio DatagramProtocol wrappers
# ---------------------------------------------------------------------------

class _AuthProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: RadiusServer) -> None:
        self._server = server

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._server._set_transport_auth(transport)
        logger.info("RADIUS auth listener ready on UDP 1812")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            self._server.handle_auth(data, addr)
        except Exception as exc:
            logger.error("RADIUS auth handler error from %s: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        logger.warning("RADIUS auth socket error: %s", exc)


class _AcctProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: RadiusServer) -> None:
        self._server = server

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._server._set_transport_acct(transport)
        logger.info("RADIUS accounting listener ready on UDP 1813")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            self._server.handle_acct(data, addr)
        except Exception as exc:
            logger.error("RADIUS acct handler error from %s: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        logger.warning("RADIUS acct socket error: %s", exc)


# ---------------------------------------------------------------------------
# Start helpers
# ---------------------------------------------------------------------------

async def start_radius_server(
    server: RadiusServer,
    host:   str = "0.0.0.0",
    auth_port: int = 1812,
    acct_port: int = 1813,
) -> tuple[asyncio.BaseTransport, asyncio.BaseTransport]:
    """
    Bind the RADIUS server to UDP 1812 and 1813.

    Returns the two transports so the caller can close them on shutdown.
    Raises OSError if the ports are already in use (e.g. a real RADIUS
    server is running on the same host).
    """
    loop = asyncio.get_event_loop()

    auth_transport, _ = await loop.create_datagram_endpoint(
        lambda: _AuthProtocol(server),
        local_addr=(host, auth_port),
    )
    acct_transport, _ = await loop.create_datagram_endpoint(
        lambda: _AcctProtocol(server),
        local_addr=(host, acct_port),
    )
    return auth_transport, acct_transport


# ---------------------------------------------------------------------------
# Module-level singleton (used by main.py)
# ---------------------------------------------------------------------------

radius_server = RadiusServer()
