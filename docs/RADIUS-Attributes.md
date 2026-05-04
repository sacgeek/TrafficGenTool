# NetLab RADIUS Attribute Reference

This document describes every RADIUS attribute sent and received by the NetLab
traffic generator.  Packets conform to RFC 2865 (authentication) and RFC 2866
(accounting) and have been validated for compatibility with Aruba ClearPass.

---

## Overview

NetLab simulates RADIUS user authentication at the start of every test session
and sends accounting logoff packets when the session ends.  Each simulated user
(alias IP) is assigned a fictitious username and an Aruba User Role from the
configured role list.

**Ports**

| Port | Protocol | Purpose |
|------|----------|---------|
| 1812 | UDP | RADIUS Authentication (Access-Request / Access-Accept) |
| 1813 | UDP | RADIUS Accounting (Accounting-Request Start / Stop) |

**Modes**

| Mode | Description |
|------|-------------|
| Internal | Controller acts as RADIUS server. Responds with Aruba VSA. All auth records visible in the dashboard. |
| External (ClearPass) | Agents send directly to an external RADIUS server. Controller assigns usernames/roles and logs dispatch only. |

The mode is determined at session launch: if **RADIUS Server IP** matches the
controller's own IP (or is left blank), internal mode is used.  Any other IP
activates external mode and the controller's UDP listeners remain idle.

---

## Access-Request  (agent → RADIUS server, UDP 1812)

Sent once per simulated user, from that user's alias IP, before any traffic
workers start.

| # | Attribute | Type | Value | Required | Notes |
|---|-----------|------|-------|----------|-------|
| 1 | User-Name | String | `alex.chen` | **Required** | Assigned from the 100-user pool at plan dispatch |
| 2 | User-Password | String | *(obfuscated)* | **Required** | Dummy value `netlab-sim`, encrypted with MD5 per RFC 2865 §5.2 |
| 4 | NAS-IP-Address | IP Address | `192.168.1.10` | **Required** | Agent node's primary outbound IP (not the alias IP) |
| 5 | NAS-Port | Integer | `0…99` | Optional | Slot index of the simulated user within this node |
| 6 | Service-Type | Integer | `2` (Framed) | **Required** | Standard framed-user login |
| 8 | Framed-IP-Address | IP Address | `10.0.0.101` | **Required** | The alias IP assigned to this simulated user |
| 26 | Vendor-Specific | — | — | — | Not sent in Access-Request |
| 30 | Called-Station-Id | String | `netlab-nas` | Optional | Static string; can be used for SSID matching in ClearPass |
| 31 | Calling-Station-Id | String | `AA:BB:0A:00:00:65` | **Required** | Deterministic MAC derived from alias IP (see MAC derivation below) |
| 32 | NAS-Identifier | String | `netlab-controller` | **Required** | Configurable; scope ClearPass policy to this string to avoid matching real users |
| 61 | NAS-Port-Type | Integer | `15` or `19` | **Required** | `15` = Ethernet, `19` = Wireless IEEE 802.11 — configurable per session |

---

## Access-Accept  (RADIUS server → agent, UDP 1812)

Sent by the NetLab controller in internal mode.  In external/ClearPass mode,
this response comes from ClearPass and may include additional attributes
depending on the configured enforcement profile.

| # | Attribute | Type | Value | Notes |
|---|-----------|------|-------|-------|
| 6 | Service-Type | Integer | `2` (Framed) | Standard framed response |
| 8 | Framed-IP-Address | IP Address | `10.0.0.101` | **Always included.** Echoes the Framed-IP-Address from the Access-Request so ClearPass and Aruba infrastructure can correlate the user's IP with their assigned role |
| 26 | Vendor-Specific | VSA | vendor=`14823`, attr=`1`, value=`"Employee"` | **Aruba-User-Role VSA** (VSA ID `14823_1`). The role string is taken from the session's active role list, assigned round-robin across users |

> **ClearPass note:** ClearPass sends the same Aruba VSA structure (vendor 14823,
> attr 1 / VSA ID `14823_1`) in its Access-Accept responses.  NetLab's internal
> server uses an identical packet layout so the agent's parsing code works in both modes.

> **Access-Reject:** In internal mode, NetLab never rejects a valid Access-Request.
> ClearPass may reject if the shared secret is wrong or the NAS-Identifier does
> not match a configured network device entry.

---

## Accounting-Request: Start  (agent → RADIUS server, UDP 1813)

Sent immediately after a successful Access-Accept, before traffic workers start.

| # | Attribute | Type | Value | Required | Notes |
|---|-----------|------|-------|----------|-------|
| 1 | User-Name | String | `alex.chen` | **Required** | Same username as Access-Request |
| 4 | NAS-IP-Address | IP Address | `192.168.1.10` | **Required** | Agent node's primary IP |
| 8 | Framed-IP-Address | IP Address | `10.0.0.101` | **Required** | User's alias IP. ClearPass correlates auth and accounting records on this attribute |
| 31 | Calling-Station-Id | String | `AA:BB:0A:00:00:65` | **Required** | Same MAC as Access-Request |
| 32 | NAS-Identifier | String | `netlab-controller` | **Required** | Must match the value in the Access-Request |
| 40 | Acct-Status-Type | Integer | `1` (Start) | **Required** | Signals session start |
| 44 | Acct-Session-Id | String | `a1b2c3d4-u5` | **Required** | Stable identifier across the Start/Stop pair. Format: `{plan_id}-u{user_index}` |
| 61 | NAS-Port-Type | Integer | `15` or `19` | Optional | Matches the value in the Access-Request |

---

## Accounting-Response  (RADIUS server → agent, UDP 1813)

Returned by the RADIUS server (internal or ClearPass) in reply to any
Accounting-Request.  Contains no attributes — just the packet header with a
computed Response Authenticator.

| Field | Value |
|-------|-------|
| Code | `5` (Accounting-Response) |
| Identifier | Mirrors the Accounting-Request identifier |
| Attributes | None required |

---

## Accounting-Request: Stop  (agent → RADIUS server, UDP 1813)

Sent after all traffic workers have stopped, before IP aliases are removed.
This ordering ensures packets depart from the correct source IPs.

| # | Attribute | Type | Value | Required | Notes |
|---|-----------|------|-------|----------|-------|
| 1 | User-Name | String | `alex.chen` | **Required** | |
| 4 | NAS-IP-Address | IP Address | `192.168.1.10` | **Required** | |
| 8 | Framed-IP-Address | IP Address | `10.0.0.101` | **Required** | |
| 31 | Calling-Station-Id | String | `AA:BB:0A:00:00:65` | **Required** | |
| 32 | NAS-Identifier | String | `netlab-controller` | **Required** | |
| 40 | Acct-Status-Type | Integer | `2` (Stop) | **Required** | Signals session end |
| 42 | Acct-Input-Octets | Integer | `0` | Stop only | Simulated — set to zero. ClearPass accepts this value. |
| 43 | Acct-Output-Octets | Integer | `0` | Stop only | Simulated — set to zero. |
| 44 | Acct-Session-Id | String | `a1b2c3d4-u5` | **Required** | Must match the Start packet |
| 46 | Acct-Session-Time | Integer | `elapsed seconds` | Stop only | Seconds from Accounting-Start to Stop |
| 61 | NAS-Port-Type | Integer | `15` or `19` | Optional | |

---

## Aruba User-Role VSA Detail

The Aruba User-Role is encoded as a Vendor-Specific Attribute (RFC 2865 §5.26):

```
Type   : 26  (Vendor-Specific)
Length : 2 + 4 + 2 + len(role_string)
Value  :
  ├─ Vendor-Id    : 0x00003967  (14823 decimal, big-endian)
  ├─ Vendor-Type  : 1           (Aruba-User-Role  —  VSA ID 14823_1)
  ├─ Vendor-Length: 2 + len(role_string)
  └─ Vendor-Value : UTF-8 role string  e.g. "Employee"
```

**Default roles** (configurable per session in the dashboard):

| Role | Typical use |
|------|-------------|
| Employee | Standard corporate user |
| HR | Human Resources — may have access to HR systems |
| CEO | Executive — elevated access profile |
| IOT | IoT devices — restricted, typically VLAN-isolated |
| GUEST | Guest / unauthenticated — internet-only or captive portal |

Custom roles can be added in the SetupPanel and are sent verbatim as the VSA
value.  Role names are case-sensitive and must match the Aruba role name exactly
as configured on the controller or ClearPass enforcement profile.

---

## MAC Address Derivation

Each alias IP is mapped to a deterministic MAC address used as
`Calling-Station-Id`.  This allows ClearPass to build consistent endpoint
identity records across multiple test sessions.

**Format:** `AA:BB:<hex-octet-1>:<hex-octet-2>:<hex-octet-3>:<hex-octet-4>`

**Examples:**

| Alias IP | Calling-Station-Id |
|----------|--------------------|
| `10.0.0.101` | `AA:BB:0A:00:00:65` |
| `192.168.1.50` | `AA:BB:C0:A8:01:32` |
| `172.16.5.200` | `AA:BB:AC:10:05:C8` |

The `AA:BB` prefix is fixed.  Addresses in this range are locally administered
(the LSB of the first octet is 1) and will not conflict with real OUI-assigned
MACs on the network.

---

## Packet Authenticator Rules

| Packet Type | Authenticator Field |
|-------------|---------------------|
| Access-Request | 16 random bytes (generated by `os.urandom(16)`) |
| Access-Accept / Reject | `MD5(Code + ID + Length + RequestAuth + Attrs + Secret)` |
| Accounting-Request | `MD5(Code + ID + Length + 16×0x00 + Attrs + Secret)` |
| Accounting-Response | `MD5(Code + ID + Length + RequestAuth + Attrs + Secret)` |

All authenticators use MD5 per RFC 2865 §3 and RFC 2866 §3.

---

## ClearPass Compatibility Notes

- All attributes follow standard RADIUS RFCs — no proprietary encoding.
- `NAS-Identifier = "netlab-controller"` (configurable) lets a ClearPass
  administrator scope enforcement policies to NetLab traffic without affecting
  real users.  Create a Network Device entry in ClearPass matching this
  identifier and the configured shared secret.
- `NAS-Port-Type` should be set to `19` (Wireless 802.11) if testing policies
  that apply only to wireless clients.
- `Acct-Input-Octets` and `Acct-Output-Octets` are sent as zero in Accounting-Stop
  packets.  ClearPass records these values but does not reject zero-byte sessions.
- PAP authentication (User-Password, RFC 2865 §5.2 obfuscation) is used.
  ClearPass must have PAP enabled for the relevant authentication source.  The
  dummy password `netlab-sim` does not need to exist in any user store — configure
  ClearPass to authenticate against a "Static Hosts" or "Endpoints" database for
  NetLab's NAS-Identifier to accept all requests unconditionally.

---

*Last updated: auto-generated during RADIUS feature implementation.*
