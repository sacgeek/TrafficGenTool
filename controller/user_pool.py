"""
NetLab RADIUS user pool.

Provides a fixed pool of 100 fictitious usernames and the assign_users()
helper that maps a list of alias IPs to RadiusUser objects, round-robining
the supplied role list and deriving deterministic MAC addresses from each IP.
"""

from __future__ import annotations

from controller.models import RadiusUser

# ---------------------------------------------------------------------------
# 100 fictitious usernames  (10 first-names × 10 last-names)
# ---------------------------------------------------------------------------

_FIRST_NAMES = [
    "alex", "blake", "casey", "drew", "elliot",
    "finley", "gael", "harper", "indira", "jaden",
]

_LAST_NAMES = [
    "chen", "garcia", "jones", "kim", "lee",
    "martin", "nguyen", "patel", "robinson", "smith",
]

# Generates exactly 100 entries: alex.chen … jaden.smith
USER_POOL: list[str] = [
    f"{first}.{last}"
    for first in _FIRST_NAMES
    for last  in _LAST_NAMES
]

assert len(USER_POOL) == 100, "USER_POOL must contain exactly 100 entries"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ip_to_mac(ip: str) -> str:
    """
    Derive a deterministic, ClearPass-friendly MAC from an IPv4 address.

    Format: AA:BB:<hex-octet-1>:<hex-octet-2>:<hex-octet-3>:<hex-octet-4>

    Example:
        10.0.0.101  →  AA:BB:0A:00:00:65
    """
    parts = ip.split(".")
    return "AA:BB:{:02X}:{:02X}:{:02X}:{:02X}".format(*map(int, parts))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assign_users(
    ip_list:  list[str],
    roles:    list[str],
    plan_id:  str = "plan",
) -> list[RadiusUser]:
    """
    Assign a username and role to every IP in ip_list.

    - Usernames are drawn sequentially from USER_POOL (wraps at 100).
    - Roles are assigned round-robin across the active role list.
    - Each user gets a stable Acct-Session-Id of "<plan_id>-u<index>".

    Args:
        ip_list:  Ordered list of alias IP strings to assign users to.
        roles:    List of Aruba role names to cycle through (must be non-empty).
        plan_id:  Short plan identifier used to build Acct-Session-Id values.

    Returns:
        List of RadiusUser objects, one per IP, in the same order as ip_list.
    """
    if not roles:
        roles = ["Employee"]

    result: list[RadiusUser] = []
    for idx, ip in enumerate(ip_list):
        result.append(RadiusUser(
            username        = USER_POOL[idx % len(USER_POOL)],
            ip_address      = ip,
            mac_address     = _ip_to_mac(ip),
            aruba_role      = roles[idx % len(roles)],
            acct_session_id = f"{plan_id}-u{idx}",
        ))
    return result
