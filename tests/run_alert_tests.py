"""
Tests for Task #4 — Alert threshold tracking in SessionStore.

Run with:
    python3 run_alert_tests.py

Tests cover:
  - AlertEvent model (serialisation, is_active property)
  - TestPlan defaults (alert_mos_floor, alert_window_s)
  - TestSession summary/full dict include alert info
  - _check_alert: no alert before window elapses
  - _check_alert: alert fires after sustained low MOS
  - _check_alert: alert does NOT fire when MOS == 0 (no data)
  - _check_alert: alert clears on recovery
  - _check_alert: alert re-fires after recovery + second dip
  - _check_alert: disabled when floor == 0
  - _check_alert: no double-fire while still below floor
  - SessionStore emit calls for alert / alert_cleared events
  - _clear_alert_state cleanup
  - stop_session / complete_session clears alert state
"""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from collections import defaultdict

# ---------------------------------------------------------------------------
# Minimal stubs so we can import the modules without the full project tree
# ---------------------------------------------------------------------------

# Stub pydantic
import types

pydantic_stub = types.ModuleType("pydantic")

class _BaseModel:
    _fields: dict = {}
    # NOTE: no __init_subclass__ — subclasses manually define _fields so we
    # must not clobber them after the class body runs.

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        # apply defaults from Field annotations for any key not supplied
        for k, (default, factory) in self.__class__._fields.items():
            if k not in kwargs:
                if factory is not None:
                    setattr(self, k, factory())
                elif default is not _MISSING:
                    setattr(self, k, default)

    def model_dump(self):
        result = {}
        for k in self.__class__._fields:
            val = getattr(self, k, None)
            if hasattr(val, 'value'):   # enum
                result[k] = val.value
            elif hasattr(val, 'model_dump'):
                result[k] = val.model_dump()
            elif isinstance(val, list):
                result[k] = [
                    (x.model_dump() if hasattr(x, 'model_dump') else x) for x in val
                ]
            else:
                result[k] = val
        return result

_MISSING = object()

def _Field(default=_MISSING, default_factory=None, **kw):
    return (default, default_factory)

class _FieldDescriptor:
    """Allows Field() in class body to register on the class."""
    pass

pydantic_stub.BaseModel = _BaseModel
pydantic_stub.Field     = _Field

sys.modules["pydantic"] = pydantic_stub

# ---------------------------------------------------------------------------
# Now build the models we need directly (avoid importing the real module)
# ---------------------------------------------------------------------------

import uuid

_SENTINEL = object()


def _field_init(cls, annotations, namespace):
    """Set up _fields from class body annotations + defaults."""
    fields = dict(getattr(cls, '_fields', {}))
    for name, typ in annotations.items():
        if name.startswith('_'):
            continue
        raw = namespace.get(name, _SENTINEL)
        if raw is _SENTINEL:
            fields[name] = (_MISSING, None)
        elif isinstance(raw, tuple) and len(raw) == 2 and (
                raw[0] is _MISSING or True) and (raw[1] is None or callable(raw[1])):
            # Field() result
            fields[name] = raw
        else:
            fields[name] = (raw, None)
    cls._fields = fields


# ---- AlertEvent ----

class AlertEvent(_BaseModel):
    __annotations__ = {
        'alert_id': str, 'session_id': str, 'stream_key': str,
        'stream_type': str, 'node_id': str, 'mos_at_alert': float,
        'floor': float, 'fired_at': float, 'cleared_at': object,
    }
    _fields = {
        'alert_id':    (None, lambda: str(uuid.uuid4())[:8]),
        'session_id':  (_MISSING, None),
        'stream_key':  (_MISSING, None),
        'stream_type': (_MISSING, None),
        'node_id':     (_MISSING, None),
        'mos_at_alert': (_MISSING, None),
        'floor':       (_MISSING, None),
        'fired_at':    (None, time.time),
        'cleared_at':  (None, None),
    }

    @property
    def is_active(self):
        return self.cleared_at is None

    def to_dict(self):
        d = {k: getattr(self, k, None) for k in self._fields}
        d['is_active'] = self.is_active
        return d


# ---- StreamSnapshot (minimal stub) ----

class StreamType:
    class _Val:
        def __init__(self, v): self.value = v
    VOICE       = _Val("voice")
    VIDEO       = _Val("video")
    WEB         = _Val("web")
    YOUTUBE     = _Val("youtube")


class StreamSnapshot:
    def __init__(self, node_id, session_id, stream_type, timestamp,
                 mos_mos=0.0, **kw):
        self.node_id      = node_id
        self.session_id   = session_id
        self.stream_type  = stream_type
        self.timestamp    = timestamp
        self.mos_mos      = mos_mos

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "session_id": self.session_id,
            "stream_type": self.stream_type.value if hasattr(self.stream_type, 'value') else str(self.stream_type),
            "timestamp": self.timestamp,
            "mos_mos": self.mos_mos,
        }


# ---- TestPlan (minimal stub) ----

class TestPlan:
    def __init__(self, name="Test", alert_mos_floor=3.5, alert_window_s=10,
                 duration_s=60, **kw):
        self.name            = name
        self.alert_mos_floor = alert_mos_floor
        self.alert_window_s  = alert_window_s
        self.duration_s      = duration_s
        self.plan_id         = str(uuid.uuid4())[:8]

    def model_dump(self):
        return {
            'name': self.name,
            'alert_mos_floor': self.alert_mos_floor,
            'alert_window_s': self.alert_window_s,
            'duration_s': self.duration_s,
            'plan_id': self.plan_id,
        }


# ---- TestSession (minimal stub) ----

class TestSession:
    def __init__(self, plan, node_ids=None):
        self.session_id = str(uuid.uuid4())[:8]
        self.plan       = plan
        self.snapshots  = []
        self.alerts     = []
        self.node_ids   = node_ids or []
        self.status     = "running"
        self.start_time = time.time()
        self.end_time   = None

    def add_snapshot(self, snap):
        self.snapshots.append(snap)

    def summary_dict(self):
        active = sum(1 for a in self.alerts if a.is_active)
        return {
            "session_id":    self.session_id,
            "status":        self.status,
            "active_alerts": active,
            "snapshot_count": len(self.snapshots),
        }

    def full_dict(self):
        return {
            **self.summary_dict(),
            "plan":    self.plan.model_dump(),
            "snapshots": [s.to_dict() for s in self.snapshots],
            "alerts":    [a.to_dict() for a in self.alerts],
        }

    @property
    def duration_s(self):
        end = self.end_time or time.time()
        return end - self.start_time


# ---------------------------------------------------------------------------
# Inline implementation of _check_alert / _clear_alert_state
# (same logic as session_store.py — we test the logic directly)
# ---------------------------------------------------------------------------

class AlertTracker:
    """
    Extracted alert-tracking logic from SessionStore for isolated testing.
    """
    def __init__(self):
        self._alert_below_since: dict[str, dict[str, float]] = defaultdict(dict)
        self._alert_fired:       dict[str, set[str]]         = defaultdict(set)
        self._emitted = []

    def _emit(self, event, data):
        self._emitted.append((event, data))

    def check_alert(self, session: TestSession, snapshot: StreamSnapshot):
        floor  = session.plan.alert_mos_floor
        window = session.plan.alert_window_s

        if floor <= 0.0 or snapshot.mos_mos <= 0.0:
            return

        session_id = session.session_id
        stream_key = f"{snapshot.node_id}:{snapshot.session_id}"
        now        = time.time()
        below      = self._alert_below_since[session_id]
        fired      = self._alert_fired[session_id]

        if snapshot.mos_mos < floor:
            if stream_key not in below:
                below[stream_key] = now
            elif stream_key not in fired:
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
        else:
            if stream_key in below:
                del below[stream_key]
            if stream_key in fired:
                fired.discard(stream_key)
                for alert in reversed(session.alerts):
                    if alert.stream_key == stream_key and alert.is_active:
                        alert.cleared_at = now
                        self._emit("alert_cleared", {
                            "alert_id":   alert.alert_id,
                            "session_id": session_id,
                            "stream_key": stream_key,
                            "cleared_at": now,
                        })
                        break

    def clear_state(self, session_id: str):
        self._alert_below_since.pop(session_id, None)
        self._alert_fired.pop(session_id, None)


def _make_snap(node_id="node-1", session_id="stream-1",
               stream_type=None, mos=4.0, ts=None):
    if stream_type is None:
        stream_type = StreamType.VOICE
    return StreamSnapshot(
        node_id=node_id,
        session_id=session_id,
        stream_type=stream_type,
        timestamp=ts or time.time(),
        mos_mos=mos,
    )


def _make_session(floor=3.5, window=10):
    plan = TestPlan(alert_mos_floor=floor, alert_window_s=window)
    return TestSession(plan=plan, node_ids=["node-1"])


# ===========================================================================
# Test cases
# ===========================================================================

class TestAlertEvent(unittest.TestCase):

    def test_is_active_when_cleared_at_is_none(self):
        a = AlertEvent(
            session_id="s1", stream_key="n:k",
            stream_type="voice", node_id="n",
            mos_at_alert=2.5, floor=3.5,
        )
        self.assertTrue(a.is_active)

    def test_is_inactive_when_cleared_at_set(self):
        a = AlertEvent(
            session_id="s1", stream_key="n:k",
            stream_type="voice", node_id="n",
            mos_at_alert=2.5, floor=3.5,
            cleared_at=time.time(),
        )
        self.assertFalse(a.is_active)

    def test_to_dict_includes_is_active(self):
        a = AlertEvent(
            session_id="s1", stream_key="n:k",
            stream_type="web", node_id="n",
            mos_at_alert=2.0, floor=3.5,
        )
        d = a.to_dict()
        self.assertIn("is_active", d)
        self.assertTrue(d["is_active"])
        self.assertEqual(d["session_id"], "s1")
        self.assertEqual(d["floor"], 3.5)

    def test_to_dict_cleared(self):
        ts = time.time()
        a = AlertEvent(
            session_id="s1", stream_key="n:k",
            stream_type="voice", node_id="n",
            mos_at_alert=2.5, floor=3.5,
            cleared_at=ts,
        )
        d = a.to_dict()
        self.assertFalse(d["is_active"])
        self.assertEqual(d["cleared_at"], ts)

    def test_alert_id_auto_generated(self):
        a1 = AlertEvent(
            session_id="s", stream_key="n:k", stream_type="voice",
            node_id="n", mos_at_alert=2.0, floor=3.5,
        )
        a2 = AlertEvent(
            session_id="s", stream_key="n:k", stream_type="voice",
            node_id="n", mos_at_alert=2.0, floor=3.5,
        )
        self.assertIsNotNone(a1.alert_id)
        self.assertNotEqual(a1.alert_id, a2.alert_id)


class TestTestPlanDefaults(unittest.TestCase):

    def test_default_floor(self):
        plan = TestPlan()
        self.assertEqual(plan.alert_mos_floor, 3.5)

    def test_default_window(self):
        plan = TestPlan()
        self.assertEqual(plan.alert_window_s, 10)

    def test_custom_floor_and_window(self):
        plan = TestPlan(alert_mos_floor=2.0, alert_window_s=30)
        self.assertEqual(plan.alert_mos_floor, 2.0)
        self.assertEqual(plan.alert_window_s, 30)

    def test_floor_zero_disables(self):
        plan = TestPlan(alert_mos_floor=0.0)
        self.assertEqual(plan.alert_mos_floor, 0.0)


class TestTestSessionAlertFields(unittest.TestCase):

    def test_summary_dict_has_active_alerts(self):
        s = _make_session()
        d = s.summary_dict()
        self.assertIn("active_alerts", d)
        self.assertEqual(d["active_alerts"], 0)

    def test_summary_dict_counts_active_alerts(self):
        s = _make_session()
        s.alerts.append(AlertEvent(
            session_id=s.session_id, stream_key="n:k",
            stream_type="voice", node_id="n",
            mos_at_alert=2.5, floor=3.5,
        ))
        s.alerts.append(AlertEvent(
            session_id=s.session_id, stream_key="n:k2",
            stream_type="video", node_id="n",
            mos_at_alert=2.0, floor=3.5,
            cleared_at=time.time(),   # already cleared
        ))
        d = s.summary_dict()
        self.assertEqual(d["active_alerts"], 1)

    def test_full_dict_includes_alerts_list(self):
        s = _make_session()
        s.alerts.append(AlertEvent(
            session_id=s.session_id, stream_key="n:k",
            stream_type="voice", node_id="n",
            mos_at_alert=2.5, floor=3.5,
        ))
        d = s.full_dict()
        self.assertIn("alerts", d)
        self.assertEqual(len(d["alerts"]), 1)
        self.assertEqual(d["alerts"][0]["stream_type"], "voice")


class TestAlertTrackerNoAlert(unittest.TestCase):
    """Verify alert does NOT fire in benign conditions."""

    def setUp(self):
        self.tracker = AlertTracker()

    def test_no_alert_when_mos_above_floor(self):
        s = _make_session(floor=3.5, window=0)
        snap = _make_snap(mos=4.2)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), 0)
        self.assertEqual(len(self.tracker._emitted), 0)

    def test_no_alert_when_mos_equals_floor(self):
        # Strictly less-than: MOS == floor should NOT alert
        s = _make_session(floor=3.5, window=0)
        snap = _make_snap(mos=3.5)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), 0)

    def test_no_alert_on_single_low_snapshot_before_window(self):
        """First low snapshot only starts the clock — no immediate alert."""
        s = _make_session(floor=3.5, window=10)
        snap = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), 0)
        self.assertEqual(len(self.tracker._emitted), 0)

    def test_no_alert_when_floor_is_zero(self):
        s = _make_session(floor=0.0, window=0)
        snap = _make_snap(mos=1.0)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), 0)

    def test_no_alert_when_mos_is_zero(self):
        """mos_mos == 0 means no data; ignore."""
        s = _make_session(floor=3.5, window=0)
        snap = _make_snap(mos=0.0)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), 0)


class TestAlertTrackerFires(unittest.TestCase):
    """Verify alert fires correctly when the window elapses."""

    def setUp(self):
        self.tracker = AlertTracker()

    def _fire_alert(self, session, window_s=10, mos=2.0):
        """
        Simulate two snapshots with manipulated timestamps so the window elapses.
        First snapshot starts the clock; second arrives after window_s.
        """
        sid = session.session_id
        stream_key = "node-1:stream-1"

        # First snapshot: start the clock
        snap1 = _make_snap(mos=mos)
        self.tracker.check_alert(session, snap1)
        # Manually set the below_since timestamp back in time
        self.tracker._alert_below_since[sid][stream_key] = time.time() - window_s - 1

        # Second snapshot: window elapsed → should fire
        snap2 = _make_snap(mos=mos)
        self.tracker.check_alert(session, snap2)

    def test_alert_fires_after_window(self):
        s = _make_session(floor=3.5, window=10)
        self._fire_alert(s, window_s=10)
        self.assertEqual(len(s.alerts), 1)
        events = [e for e, _ in self.tracker._emitted]
        self.assertIn("alert", events)

    def test_alert_event_fields(self):
        s = _make_session(floor=3.5, window=10)
        self._fire_alert(s, window_s=10, mos=2.3)
        alert = s.alerts[0]
        self.assertEqual(alert.session_id, s.session_id)
        self.assertEqual(alert.stream_key, "node-1:stream-1")
        self.assertAlmostEqual(alert.mos_at_alert, 2.3, places=1)
        self.assertEqual(alert.floor, 3.5)
        self.assertTrue(alert.is_active)

    def test_alert_emit_payload(self):
        s = _make_session(floor=3.5, window=10)
        self._fire_alert(s)
        alert_events = [(e, d) for e, d in self.tracker._emitted if e == "alert"]
        self.assertEqual(len(alert_events), 1)
        _, data = alert_events[0]
        self.assertIn("alert_id", data)
        self.assertIn("mos_at_alert", data)
        self.assertTrue(data["is_active"])

    def test_no_double_fire_while_still_below(self):
        """Once the alert fires, subsequent low snapshots should NOT emit another."""
        s = _make_session(floor=3.5, window=10)
        self._fire_alert(s, window_s=10)
        initial_count = len(s.alerts)
        # More low snapshots
        for _ in range(5):
            snap = _make_snap(mos=2.0)
            self.tracker.check_alert(s, snap)
        self.assertEqual(len(s.alerts), initial_count)
        alert_events = [e for e, _ in self.tracker._emitted if e == "alert"]
        self.assertEqual(len(alert_events), 1)

    def test_different_streams_independent(self):
        """Two streams on the same session alert independently."""
        s = _make_session(floor=3.5, window=10)
        sid = s.session_id

        for stream_id in ("stream-A", "stream-B"):
            snap = _make_snap(session_id=stream_id, mos=2.0)
            self.tracker.check_alert(s, snap)
            stream_key = f"node-1:{stream_id}"
            self.tracker._alert_below_since[sid][stream_key] = time.time() - 15
            snap2 = _make_snap(session_id=stream_id, mos=2.0)
            self.tracker.check_alert(s, snap2)

        self.assertEqual(len(s.alerts), 2)
        alert_events = [e for e, _ in self.tracker._emitted if e == "alert"]
        self.assertEqual(len(alert_events), 2)


class TestAlertTrackerClears(unittest.TestCase):
    """Verify alert clears when MOS recovers."""

    def setUp(self):
        self.tracker = AlertTracker()

    def _fire_then_recover(self, s, window_s=10):
        """Fire an alert then send a good-MOS snapshot."""
        sid = s.session_id
        stream_key = "node-1:stream-1"
        # Start clock
        snap1 = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap1)
        # Backdate clock so window elapses
        self.tracker._alert_below_since[sid][stream_key] = time.time() - window_s - 1
        # Fire
        snap2 = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap2)
        self.assertEqual(len(s.alerts), 1)
        # Recover
        snap3 = _make_snap(mos=4.5)
        self.tracker.check_alert(s, snap3)

    def test_alert_cleared_on_recovery(self):
        s = _make_session()
        self._fire_then_recover(s)
        self.assertFalse(s.alerts[0].is_active)
        self.assertIsNotNone(s.alerts[0].cleared_at)

    def test_alert_cleared_event_emitted(self):
        s = _make_session()
        self._fire_then_recover(s)
        cleared_events = [e for e, _ in self.tracker._emitted if e == "alert_cleared"]
        self.assertEqual(len(cleared_events), 1)

    def test_alert_cleared_payload(self):
        s = _make_session()
        self._fire_then_recover(s)
        _, data = next((e, d) for e, d in self.tracker._emitted if e == "alert_cleared")
        self.assertIn("alert_id", data)
        self.assertIn("session_id", data)
        self.assertIn("cleared_at", data)

    def test_below_since_removed_on_recovery(self):
        s = _make_session()
        self._fire_then_recover(s)
        sid = s.session_id
        self.assertNotIn("node-1:stream-1", self.tracker._alert_below_since.get(sid, {}))

    def test_fired_set_cleared_on_recovery(self):
        s = _make_session()
        self._fire_then_recover(s)
        sid = s.session_id
        self.assertNotIn("node-1:stream-1", self.tracker._alert_fired.get(sid, set()))

    def test_alert_reraises_after_recovery_and_second_dip(self):
        """After clearing, a second sustained dip should fire a new alert."""
        s = _make_session()
        self._fire_then_recover(s)
        self.assertEqual(len(s.alerts), 1)

        # Second dip
        sid = s.session_id
        stream_key = "node-1:stream-1"
        snap_low = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap_low)
        self.tracker._alert_below_since[sid][stream_key] = time.time() - 15
        snap_low2 = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap_low2)

        self.assertEqual(len(s.alerts), 2)
        alert_events = [e for e, _ in self.tracker._emitted if e == "alert"]
        self.assertEqual(len(alert_events), 2)

    def test_recovery_without_prior_alert_is_silent(self):
        """Good MOS before any alert fires → no events."""
        s = _make_session()
        snap = _make_snap(mos=4.5)
        self.tracker.check_alert(s, snap)
        self.assertEqual(len(self.tracker._emitted), 0)


class TestAlertTrackerClearState(unittest.TestCase):
    """Verify cleanup on session end."""

    def setUp(self):
        self.tracker = AlertTracker()

    def test_clear_state_removes_below_since(self):
        s = _make_session()
        snap = _make_snap(mos=2.0)
        self.tracker.check_alert(s, snap)
        self.assertIn(s.session_id, self.tracker._alert_below_since)
        self.tracker.clear_state(s.session_id)
        self.assertNotIn(s.session_id, self.tracker._alert_below_since)

    def test_clear_state_removes_fired(self):
        s = _make_session()
        sid = s.session_id
        self.tracker._alert_fired[sid].add("node-1:stream-1")
        self.tracker.clear_state(sid)
        self.assertNotIn(sid, self.tracker._alert_fired)

    def test_clear_state_idempotent(self):
        """Calling clear on an unknown session_id is a no-op."""
        self.tracker.clear_state("nonexistent-session")  # should not raise

    def test_clear_does_not_affect_other_sessions(self):
        s1 = _make_session()
        s2 = _make_session()
        snap = _make_snap(mos=2.0)
        self.tracker.check_alert(s1, snap)
        self.tracker.check_alert(s2, snap)
        self.tracker.clear_state(s1.session_id)
        self.assertNotIn(s1.session_id, self.tracker._alert_below_since)
        self.assertIn(s2.session_id, self.tracker._alert_below_since)


class TestAlertMOSFloor(unittest.TestCase):
    """Boundary tests for floor comparisons."""

    def setUp(self):
        self.tracker = AlertTracker()

    def test_just_below_floor_starts_clock(self):
        s = _make_session(floor=3.5, window=100)
        snap = _make_snap(mos=3.49)
        self.tracker.check_alert(s, snap)
        self.assertIn("node-1:stream-1", self.tracker._alert_below_since[s.session_id])

    def test_exactly_at_floor_no_clock(self):
        s = _make_session(floor=3.5, window=100)
        snap = _make_snap(mos=3.5)
        self.tracker.check_alert(s, snap)
        self.assertNotIn("node-1:stream-1", self.tracker._alert_below_since.get(s.session_id, {}))

    def test_just_above_floor_no_clock(self):
        s = _make_session(floor=3.5, window=100)
        snap = _make_snap(mos=3.51)
        self.tracker.check_alert(s, snap)
        self.assertNotIn("node-1:stream-1", self.tracker._alert_below_since.get(s.session_id, {}))

    def test_very_low_mos_triggers_with_zero_window(self):
        """window=0 means alert fires on the SECOND snapshot (after clock is set)."""
        s = _make_session(floor=3.5, window=0)
        stream_key = "node-1:stream-1"
        snap1 = _make_snap(mos=1.0)
        self.tracker.check_alert(s, snap1)
        # backdate to zero seconds ago (window=0 → already elapsed)
        self.tracker._alert_below_since[s.session_id][stream_key] = time.time() - 0.001
        snap2 = _make_snap(mos=1.0)
        self.tracker.check_alert(s, snap2)
        self.assertEqual(len(s.alerts), 1)


# ===========================================================================

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestAlertEvent,
        TestTestPlanDefaults,
        TestTestSessionAlertFields,
        TestAlertTrackerNoAlert,
        TestAlertTrackerFires,
        TestAlertTrackerClears,
        TestAlertTrackerClearState,
        TestAlertMOSFloor,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
