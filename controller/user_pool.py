"""
NetLab RADIUS user pool.

Provides a named pool of fictitious users and the assign_users() helper that
maps a list of alias IPs to RadiusUser objects.

Role assignment rules:
  - Users with a fixed_role always receive that role, regardless of the
    session's configured role list.
  - Users without a fixed_role rotate through the session's active role list
    (round-robin, counting only the rotating users).

MAC addresses are derived deterministically from each alias IP so ClearPass
builds consistent endpoint identity records across sessions.
"""

from __future__ import annotations

from controller.models import RadiusUser

# ---------------------------------------------------------------------------
# User pool
#
# Each entry is (username, fixed_role_or_None).
# fixed_role overrides the session role list for that user every time.
# ---------------------------------------------------------------------------

USER_POOL: list[tuple[str, str | None]] = [
    ("robert.california",     "CEO"),
    ("jo.bennett",            None),
    ("david.wallace",         None),
    ("pam.beesly",            "Employee"),
    ("jim.halpert",           "Employee"),
    ("jan.levinson",          None),
    ("erin.hannon",           None),
    ("michael.scott",         "Manager"),
    ("dwight.schrute",        "Employee"),
    ("karen.filippelli",      None),
    ("andy.bernard",          "Employee"),
    ("kevin.malone",          None),
    ("kelly.kapoor",          None),
    ("cathy.simms",           None),
    ("angela.martin",         None),
    ("toby.flenderson",       None),
    ("holly.flax",            None),
    ("stanley.hudson",        None),
    ("ryan.howard",           None),
    ("gabe.lewis",            "HR"),
    ("creed.bratton",         None),
    ("phyllis.vance",         None),
    ("meredith.palmer",       None),
    ("darryl.philbin",        None),
    ("nellie.bertram",        None),
    ("oscar.martinez",        None),
    ("pete.miller",           None),
    ("roy.anderson",          None),
    ("carol.stills",          None),
    ("billy.merchant",        None),
    ("jada.philbin",          "GUEST"),
    ("hannah.smoterich-barr", None),
]


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
    ip_list: list[str],
    roles:   list[str],
    plan_id: str = "plan",
) -> list[RadiusUser]:
    """
    Assign a username and role to every IP in ip_list.

    Assignment rules:
      - Usernames are drawn sequentially from USER_POOL, wrapping if
        ip_list is longer than the pool.
      - Users with a fixed role always receive that role.
      - Users without a fixed role receive roles round-robin from *roles*,
        with the counter advancing only for rotating users so the distribution
        across roles stays even.
      - Each user receives a stable Acct-Session-Id of "<plan_id>-u<index>".

    Args:
        ip_list:  Ordered list of alias IP strings to assign users to.
        roles:    Active Aruba role names for rotating users (must be non-empty).
        plan_id:  Short plan identifier used to build Acct-Session-Id values.

    Returns:
        List of RadiusUser objects, one per IP, in the same order as ip_list.
    """
    if not roles:
        roles = ["Employee"]

    result: list[RadiusUser] = []
    rotate_counter = 0  # counts only rotating (no fixed_role) users

    for idx, ip in enumerate(ip_list):
        username, fixed_role = USER_POOL[idx % len(USER_POOL)]

        if fixed_role:
            role = fixed_role
        else:
            role = roles[rotate_counter % len(roles)]
            rotate_counter += 1

        result.append(RadiusUser(
            username        = username,
            ip_address      = ip,
            mac_address     = _ip_to_mac(ip),
            aruba_role      = role,
            acct_session_id = f"{plan_id}-u{idx}",
        ))

    return result
