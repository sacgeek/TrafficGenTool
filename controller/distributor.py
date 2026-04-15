"""
Plan distributor.

Takes a TestPlan submitted from the dashboard and:
  1. Validates enough nodes are connected.
  2. Allocates IP aliases for each simulated user on each node.
  3. Pairs up voice/video/screenshare sessions across nodes.
  4. Produces one NodePlan per worker node, ready to be dispatched via WS.

IP allocation strategy:
  Each node has a declared ip_range_start / ip_range_end.
  We assign users sequentially within that range.
  User 0 on node-1 gets ip_range_start, user 1 gets +1, etc.

Pairing strategy for calls:
  Voice/video calls are paired round-robin across all available nodes.
  If only one node is connected, calls loop back to the same node
  (useful for testing but noted in the plan).

Screenshare:
  One sender (on the first node, first user IP), all other active
  user IPs become receivers.
"""

from __future__ import annotations
import ipaddress
import logging
from dataclasses import dataclass

from controller.models import TestPlan, NodePlan, NodeState, StreamType

logger = logging.getLogger(__name__)


class PlanError(ValueError):
    """Raised when a plan cannot be distributed with available nodes."""


@dataclass
class _UserSlot:
    node_id: str
    user_index: int   # 0-based within node
    ip: str


def _ip_range(start: str, end: str) -> list[str]:
    """Return list of IPs from start to end inclusive."""
    s = ipaddress.ip_address(start)
    e = ipaddress.ip_address(end)
    result = []
    current = s
    while current <= e:
        result.append(str(current))
        current += 1
    return result


def distribute_plan(plan: TestPlan, nodes: list[NodeState]) -> dict[str, NodePlan]:
    """
    Build per-node plans from a TestPlan.

    Returns
    -------
    dict mapping node_id → NodePlan
    """
    if not nodes:
        raise PlanError("No worker nodes connected.")

    total_users = max(
        plan.web_users,
        plan.youtube_users,
        plan.voice_calls * 2,
        plan.video_calls * 2,
        plan.screen_shares + 1,  # at least 1 sender + viewers
    )

    # Build per-node IP slot pools
    node_slots: dict[str, list[str]] = {}
    for node in nodes:
        ips = _ip_range(node.info.ip_range_start, node.info.ip_range_end)
        if len(ips) < plan.web_users // max(1, len(nodes)):
            logger.warning(
                "Node %s has only %d IPs, may not satisfy all users",
                node.node_id, len(ips),
            )
        node_slots[node.node_id] = ips

    # Initialise empty NodePlan per node
    node_plans: dict[str, NodePlan] = {
        node.node_id: NodePlan(
            plan_id       = plan.plan_id,
            node_id       = node.node_id,
            duration_s    = plan.duration_s,
            web_urls      = plan.web_urls,
            youtube_url   = plan.youtube_url,
        )
        for node in nodes
    }

    # ------------------------------------------------------------------
    # Web browsing: distribute users evenly across nodes
    # ------------------------------------------------------------------
    if plan.web_users > 0:
        users_per_node = plan.web_users // len(nodes)
        remainder      = plan.web_users % len(nodes)
        for i, node in enumerate(nodes):
            count = users_per_node + (1 if i < remainder else 0)
            node_plans[node.node_id].web_users = count

    # ------------------------------------------------------------------
    # YouTube: same distribution as web
    # ------------------------------------------------------------------
    if plan.youtube_users > 0:
        users_per_node = plan.youtube_users // len(nodes)
        remainder      = plan.youtube_users % len(nodes)
        for i, node in enumerate(nodes):
            count = users_per_node + (1 if i < remainder else 0)
            node_plans[node.node_id].youtube_users = count

    # ------------------------------------------------------------------
    # Build an interleaved list of (node_id, ip) user slots for UDP sessions.
    # Interleaving ensures consecutive slot picks for a call pair land on
    # different nodes: [n1-ip1, n2-ip1, n1-ip2, n2-ip2, ...]
    # A flat list (all of n1 then all of n2) would put both call sides
    # on the same node for the first several sessions.
    # ------------------------------------------------------------------
    all_slots: list[_UserSlot] = []
    max_ips = max(len(ips) for ips in node_slots.values())
    for slot_idx in range(max_ips):
        for node in nodes:
            ips = node_slots[node.node_id]
            if slot_idx < len(ips):
                all_slots.append(_UserSlot(node_id=node.node_id, user_index=slot_idx, ip=ips[slot_idx]))

    slot_cursor = 0

    def next_slot() -> _UserSlot:
        nonlocal slot_cursor
        if not all_slots:
            raise PlanError("No IP slots available for UDP sessions.")
        slot = all_slots[slot_cursor % len(all_slots)]
        slot_cursor += 1
        return slot

    # ------------------------------------------------------------------
    # Voice calls: bidirectional pairs
    # ------------------------------------------------------------------
    for call_idx in range(plan.voice_calls):
        session_id = f"voice-{plan.plan_id}-{call_idx}"
        a = next_slot()
        b = next_slot()
        _add_udp_session(node_plans, session_id, "voice", a, b, bidirectional=True)

    # ------------------------------------------------------------------
    # Video calls: bidirectional pairs
    # ------------------------------------------------------------------
    for call_idx in range(plan.video_calls):
        session_id = f"video-{plan.plan_id}-{call_idx}"
        a = next_slot()
        b = next_slot()
        _add_udp_session(node_plans, session_id, "video", a, b, bidirectional=True)

    # ------------------------------------------------------------------
    # Screen shares: one sender, all subsequent slots are receivers
    # ------------------------------------------------------------------
    for share_idx in range(plan.screen_shares):
        session_id = f"screen-{plan.plan_id}-{share_idx}"
        sender = next_slot()

        # Number of receivers: use remaining slots up to a max of 4
        # to avoid flooding small test environments
        receivers: list[_UserSlot] = []
        viewer_count = min(4, len(all_slots) - 1)
        for _ in range(max(1, viewer_count)):
            receivers.append(next_slot())

        # Sender
        node_plans[sender.node_id].udp_sessions.append({
            "session_id":  session_id,
            "stream_type": "screenshare",
            "role":        "sender",
            "local_ip":    sender.ip,
            "peer_ips":    [r.ip for r in receivers],
            "peer_ports":  [3480] * len(receivers),
        })

        # Receivers
        for recv in receivers:
            node_plans[recv.node_id].udp_sessions.append({
                "session_id":  session_id,
                "stream_type": "screenshare",
                "role":        "receiver",
                "local_ip":    recv.ip,
                "peer_ip":     sender.ip,
                "peer_port":   3480,
            })

    logger.info(
        "Plan %s distributed to %d nodes: voice=%d video=%d screen=%d web=%d yt=%d",
        plan.plan_id, len(nodes),
        plan.voice_calls, plan.video_calls, plan.screen_shares,
        plan.web_users, plan.youtube_users,
    )

    return node_plans


def _add_udp_session(
    node_plans: dict[str, NodePlan],
    session_id: str,
    stream_type: str,
    a: _UserSlot,
    b: _UserSlot,
    bidirectional: bool,
) -> None:
    """Add sender/receiver entries to both nodes for a paired session."""
    # Node A: sends to B, receives from B
    node_plans[a.node_id].udp_sessions.append({
        "session_id":  session_id,
        "stream_type": stream_type,
        "role":        "both" if bidirectional else "sender",
        "local_ip":    a.ip,
        "peer_ip":     b.ip,
    })
    # Node B: mirror
    node_plans[b.node_id].udp_sessions.append({
        "session_id":  session_id,
        "stream_type": stream_type,
        "role":        "both" if bidirectional else "receiver",
        "local_ip":    b.ip,
        "peer_ip":     a.ip,
    })
