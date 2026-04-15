"""
Controller logic tests — stdlib only (no pydantic/fastapi required).
Run with: python3 tests/run_controller_tests.py
"""
import sys, os, time, csv, io, unittest, uuid as _uuid
from dataclasses import dataclass, field
from collections import defaultdict
import ipaddress

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- Lightweight model stand-ins ----

class StreamType:
    VOICE="voice"; VIDEO="video"; SCREENSHARE="screenshare"; WEB="web"; YOUTUBE="youtube"

class NodeStatus:
    CONNECTED="connected"; RUNNING="running"; IDLE="idle"; ERROR="error"; OFFLINE="offline"

class SessionStatus:
    PENDING="pending"; RUNNING="running"; STOPPED="stopped"; COMPLETE="complete"

@dataclass
class AgentInfo:
    node_id:str; hostname:str; ip_range_start:str; ip_range_end:str
    max_users:int=20; capabilities:list=field(default_factory=lambda:["voice","video","screenshare","web","youtube"]); agent_version:str="1.0.0"

@dataclass
class NodeState:
    node_id:str; info:AgentInfo; status:str=NodeStatus.IDLE
    connected_at:float=field(default_factory=time.time); last_seen:float=field(default_factory=time.time)
    current_plan_id:str=None
    def to_dict(self): return {"node_id":self.node_id,"hostname":self.info.hostname,"status":self.status,"ip_range":f"{self.info.ip_range_start}-{self.info.ip_range_end}"}

@dataclass
class StreamSnapshot:
    node_id:str; session_id:str; stream_type:str; timestamp:float
    loss_pct:float=0.0; latency_ms:float=0.0; jitter_ms:float=0.0
    mos_mos:float=0.0; mos_label:str="unknown"; mos_color:str="gray"; mos_r_factor:float=0.0
    packets_recv:int=0; packets_expected:int=0; page_load_ms:float=None; bytes_downloaded:int=None
    def to_dict(self): return self.__dict__.copy()

@dataclass
class TestPlan:
    name:str="test"; plan_id:str=field(default_factory=lambda:str(_uuid.uuid4())[:8])
    web_users:int=0; web_urls:list=field(default_factory=list); youtube_users:int=0
    voice_calls:int=0; video_calls:int=0; screen_shares:int=0; duration_s:int=60
    node_ids:list=field(default_factory=list); created_at:float=field(default_factory=time.time)
    def model_dump(self): return self.__dict__.copy()

@dataclass
class TestSession:
    plan:TestPlan; session_id:str=field(default_factory=lambda:str(_uuid.uuid4())[:8])
    status:str=SessionStatus.PENDING; start_time:float=None; end_time:float=None
    snapshots:list=field(default_factory=list); node_ids:list=field(default_factory=list)
    @property
    def duration_s(self): return (self.end_time or time.time()) - self.start_time if self.start_time else None
    def add_snapshot(self, s): self.snapshots.append(s)
    def summary_dict(self): return {"session_id":self.session_id,"plan_name":self.plan.name,"status":self.status,"start_time":self.start_time,"end_time":self.end_time,"duration_s":self.duration_s,"node_ids":self.node_ids,"snapshot_count":len(self.snapshots)}
    def full_dict(self): return {**self.summary_dict(),"snapshots":[s.to_dict() for s in self.snapshots]}

@dataclass
class NodePlan:
    plan_id:str; node_id:str; duration_s:int; web_users:int=0
    web_urls:list=field(default_factory=list); youtube_users:int=0
    udp_sessions:list=field(default_factory=list)
    def model_dump(self): return self.__dict__.copy()

# ---- SessionStore ----

class SessionStore:
    def __init__(self):
        self._nodes={}; self._sessions={}; self._listeners=defaultdict(list)
    def register_node(self,nid,info):
        n=NodeState(node_id=nid,info=info); self._nodes[nid]=n; self._emit("node_update",n.to_dict()); return n
    def get_node(self,nid): return self._nodes.get(nid)
    def list_nodes(self): return list(self._nodes.values())
    def update_node_status(self,nid,status):
        n=self._nodes.get(nid)
        if n: n.status=status; n.last_seen=time.time(); self._emit("node_update",n.to_dict())
    def touch_node(self,nid):
        n=self._nodes.get(nid)
        if n: n.last_seen=time.time()
    def remove_node(self,nid): self._nodes.pop(nid,None); self._emit("node_update",{"node_id":nid,"status":"offline"})
    def connected_node_ids(self): return [nid for nid,n in self._nodes.items() if n.status!=NodeStatus.OFFLINE]
    def create_session(self,plan,node_ids):
        s=TestSession(plan=plan,node_ids=node_ids); self._sessions[s.session_id]=s; self._emit("session_update",s.summary_dict()); return s
    def get_session(self,sid): return self._sessions.get(sid)
    def get_active_session(self):
        for s in self._sessions.values():
            if s.status in (SessionStatus.PENDING,SessionStatus.RUNNING): return s
        return None
    def list_sessions(self): return sorted(self._sessions.values(),key=lambda s:s.start_time or 0,reverse=True)
    def start_session(self,sid):
        s=self._sessions.get(sid)
        if s: s.status=SessionStatus.RUNNING; s.start_time=time.time(); self._emit("session_update",s.summary_dict())
    def stop_session(self,sid):
        s=self._sessions.get(sid)
        if s: s.status=SessionStatus.STOPPED; s.end_time=time.time(); self._emit("session_update",s.summary_dict())
    def complete_session(self,sid):
        s=self._sessions.get(sid)
        if s: s.status=SessionStatus.COMPLETE; s.end_time=time.time(); self._emit("session_update",s.summary_dict())
    def clear_sessions(self):
        to_rm=[sid for sid,s in self._sessions.items() if s.status in (SessionStatus.COMPLETE,SessionStatus.STOPPED)]
        for sid in to_rm: del self._sessions[sid]
        return len(to_rm)
    def add_snapshot(self,sid,snap):
        s=self._sessions.get(sid)
        if s: s.add_snapshot(snap); self._emit("snapshot",snap.to_dict())
    def subscribe(self,event,cb): self._listeners[event].append(cb)
    def _emit(self,event,data):
        for cb in self._listeners.get(event,[]): 
            try: cb(event,data)
            except: pass
    def stats(self): return {"nodes_connected":len(self._nodes),"sessions_total":len(self._sessions),"sessions_active":sum(1 for s in self._sessions.values() if s.status==SessionStatus.RUNNING),"snapshots_total":sum(len(s.snapshots) for s in self._sessions.values())}

# ---- Distributor ----

class PlanError(ValueError): pass

def _ip_range(start,end):
    s,e=ipaddress.ip_address(start),ipaddress.ip_address(end)
    result,cur=[],s
    while cur<=e: result.append(str(cur)); cur+=1
    return result

def distribute_plan(plan,nodes):
    if not nodes: raise PlanError("No worker nodes connected.")
    node_slots={n.node_id:_ip_range(n.info.ip_range_start,n.info.ip_range_end) for n in nodes}
    node_plans={n.node_id:NodePlan(plan_id=plan.plan_id,node_id=n.node_id,duration_s=plan.duration_s,web_urls=plan.web_urls) for n in nodes}
    if plan.web_users:
        per=plan.web_users//len(nodes); rem=plan.web_users%len(nodes)
        for i,n in enumerate(nodes): node_plans[n.node_id].web_users=per+(1 if i<rem else 0)
    if plan.youtube_users:
        per=plan.youtube_users//len(nodes); rem=plan.youtube_users%len(nodes)
        for i,n in enumerate(nodes): node_plans[n.node_id].youtube_users=per+(1 if i<rem else 0)
    max_ips=max(len(v) for v in node_slots.values())
    all_slots=[]
    for si in range(max_ips):
        for n in nodes:
            if si < len(node_slots[n.node_id]): all_slots.append({"node_id":n.node_id,"ip":node_slots[n.node_id][si]})
    cur=[0]
    def next_slot():
        if not all_slots: raise PlanError("No IP slots.")
        s=all_slots[cur[0]%len(all_slots)]; cur[0]+=1; return s
    for i in range(plan.voice_calls):
        sid=f"voice-{plan.plan_id}-{i}"; a,b=next_slot(),next_slot()
        for slot,peer in [(a,b),(b,a)]:
            node_plans[slot["node_id"]].udp_sessions.append({"session_id":sid,"stream_type":"voice","role":"both","local_ip":slot["ip"],"peer_ip":peer["ip"]})
    for i in range(plan.video_calls):
        sid=f"video-{plan.plan_id}-{i}"; a,b=next_slot(),next_slot()
        for slot,peer in [(a,b),(b,a)]:
            node_plans[slot["node_id"]].udp_sessions.append({"session_id":sid,"stream_type":"video","role":"both","local_ip":slot["ip"],"peer_ip":peer["ip"]})
    for i in range(plan.screen_shares):
        sid=f"screen-{plan.plan_id}-{i}"; sender=next_slot()
        receivers=[next_slot() for _ in range(min(4,len(all_slots)-1))]
        node_plans[sender["node_id"]].udp_sessions.append({"session_id":sid,"stream_type":"screenshare","role":"sender","local_ip":sender["ip"],"peer_ips":[r["ip"] for r in receivers]})
        for r in receivers:
            node_plans[r["node_id"]].udp_sessions.append({"session_id":sid,"stream_type":"screenshare","role":"receiver","local_ip":r["ip"],"peer_ip":sender["ip"]})
    return node_plans

# ---- Export ----

CSV_FIELDS=["session_id","node_id","stream_type","timestamp","loss_pct","latency_ms","jitter_ms","mos_mos","mos_label","mos_color","mos_r_factor","packets_recv","packets_expected","page_load_ms","bytes_downloaded"]

def export_csv(session):
    buf=io.StringIO(); w=csv.DictWriter(buf,fieldnames=CSV_FIELDS,extrasaction="ignore"); w.writeheader()
    for snap in session.snapshots:
        row=snap.to_dict(); row["session_id"]=session.session_id; w.writerow(row)
    return buf.getvalue().encode()

def _compute_stats(snaps):
    if not snaps: return {}
    loss=[s.loss_pct for s in snaps]; lat=[s.latency_ms for s in snaps if s.latency_ms>0]
    jitter=[s.jitter_ms for s in snaps]; mos=[s.mos_mos for s in snaps if s.mos_mos>0]
    avg=lambda l:sum(l)/len(l) if l else 0.0
    return {"count":len(snaps),"loss_avg":round(avg(loss),2),"loss_max":round(max(loss),2) if loss else 0.0,"latency_avg":round(avg(lat),1),"latency_max":round(max(lat),1) if lat else 0.0,"jitter_avg":round(avg(jitter),1),"jitter_max":round(max(jitter),1) if jitter else 0.0,"mos_avg":round(avg(mos),2),"mos_min":round(min(mos),2) if mos else 0.0}

# ---- Helpers ----

def _make_agent(nid="n1",start="192.168.1.101",end="192.168.1.110"):
    return AgentInfo(node_id=nid,hostname=f"host-{nid}",ip_range_start=start,ip_range_end=end)

def _make_node(nid,s,e):
    return NodeState(node_id=nid,info=_make_agent(nid,s,e))

def _make_session(n=5,status=SessionStatus.COMPLETE):
    plan=TestPlan(name="t",voice_calls=1)
    sess=TestSession(plan=plan,node_ids=["n1"],status=status,start_time=time.time()-60,end_time=time.time())
    for i in range(n):
        sess.add_snapshot(StreamSnapshot("n1",sess.session_id,StreamType.VOICE,time.time(),loss_pct=float(i%5),latency_ms=30.0+i,jitter_ms=2.0+i*0.5,mos_mos=4.0-i*0.1,mos_label="good",mos_color="green",mos_r_factor=85.0))
    return sess

# ============================================================
# TESTS
# ============================================================

class TestModels(unittest.TestCase):
    def test_plan_defaults(self): p=TestPlan(); self.assertEqual(p.voice_calls,0); self.assertEqual(p.duration_s,60)
    def test_plan_unique_ids(self): self.assertNotEqual(TestPlan().plan_id,TestPlan().plan_id)
    def test_snapshot_to_dict(self):
        s=StreamSnapshot("n1","s1",StreamType.VOICE,time.time(),mos_label="good")
        self.assertEqual(s.to_dict()["mos_label"],"good")
    def test_session_duration(self):
        s=TestSession(plan=TestPlan()); s.start_time=time.time()-10; self.assertGreater(s.duration_s,9)
    def test_session_summary_keys(self):
        d=TestSession(plan=TestPlan()).summary_dict()
        for k in ("session_id","plan_name","status","snapshot_count"): self.assertIn(k,d)
    def test_node_to_dict(self):
        d=_make_node("n1","10.0.0.1","10.0.0.5").to_dict(); self.assertIn("node_id",d); self.assertIn("status",d)

class TestSessionStore(unittest.TestCase):
    def setUp(self): self.store=SessionStore()
    def test_register_and_get(self): self.store.register_node("n1",_make_agent()); self.assertIsNotNone(self.store.get_node("n1"))
    def test_remove_node(self): self.store.register_node("n1",_make_agent()); self.store.remove_node("n1"); self.assertIsNone(self.store.get_node("n1"))
    def test_status_update(self): self.store.register_node("n1",_make_agent()); self.store.update_node_status("n1",NodeStatus.RUNNING); self.assertEqual(self.store.get_node("n1").status,NodeStatus.RUNNING)
    def test_create_and_get_session(self):
        s=self.store.create_session(TestPlan(),["n1"]); self.assertIsNotNone(self.store.get_session(s.session_id))
    def test_active_session(self):
        s=self.store.create_session(TestPlan(),["n1"]); self.assertEqual(self.store.get_active_session().session_id,s.session_id)
    def test_no_active_after_stop(self):
        s=self.store.create_session(TestPlan(),["n1"]); self.store.stop_session(s.session_id); self.assertIsNone(self.store.get_active_session())
    def test_add_snapshot(self):
        s=self.store.create_session(TestPlan(),["n1"])
        self.store.add_snapshot(s.session_id,StreamSnapshot("n1",s.session_id,StreamType.VOICE,time.time()))
        self.assertEqual(len(self.store.get_session(s.session_id).snapshots),1)
    def test_clear_sessions(self):
        for _ in range(3):
            s=self.store.create_session(TestPlan(),["n1"]); self.store.stop_session(s.session_id)
        self.assertEqual(self.store.clear_sessions(),3)
    def test_stats_structure(self):
        for k in ("nodes_connected","sessions_total","sessions_active","snapshots_total"): self.assertIn(k,self.store.stats())
    def test_listener_fires(self):
        events=[]; self.store.subscribe("node_update",lambda e,d:events.append(e))
        self.store.register_node("n1",_make_agent()); self.assertEqual(events,["node_update"])
    def test_list_sessions(self):
        for i in range(3): self.store.create_session(TestPlan(name=f"t{i}"),["n1"])
        self.assertEqual(len(self.store.list_sessions()),3)
    def test_start_session_marks_running(self):
        s=self.store.create_session(TestPlan(),["n1"]); self.store.start_session(s.session_id)
        self.assertEqual(self.store.get_session(s.session_id).status,SessionStatus.RUNNING)

class TestDistributor(unittest.TestCase):
    def test_no_nodes_raises(self):
        with self.assertRaises(PlanError): distribute_plan(TestPlan(voice_calls=1),[])
    def test_voice_both_nodes_get_sessions(self):
        r=distribute_plan(TestPlan(voice_calls=1),[_make_node("n1","10.0.0.1","10.0.0.10"),_make_node("n2","10.0.0.11","10.0.0.20")])
        self.assertEqual(len(r["n1"].udp_sessions),1); self.assertEqual(len(r["n2"].udp_sessions),1)
    def test_voice_roles_are_both(self):
        r=distribute_plan(TestPlan(voice_calls=1),[_make_node("n1","10.0.0.1","10.0.0.5"),_make_node("n2","10.0.0.6","10.0.0.10")])
        roles={r["n1"].udp_sessions[0]["role"],r["n2"].udp_sessions[0]["role"]}; self.assertEqual(roles,{"both"})
    def test_two_video_calls_four_sessions(self):
        r=distribute_plan(TestPlan(video_calls=2),[_make_node("n1","10.0.0.1","10.0.0.10"),_make_node("n2","10.0.0.11","10.0.0.20")])
        self.assertEqual(sum(len(np.udp_sessions) for np in r.values()),4)
    def test_screenshare_roles(self):
        r=distribute_plan(TestPlan(screen_shares=1),[_make_node("n1","10.0.0.1","10.0.0.10"),_make_node("n2","10.0.0.11","10.0.0.20")])
        roles={s["role"] for np in r.values() for s in np.udp_sessions}
        self.assertIn("sender",roles); self.assertIn("receiver",roles)
    def test_web_distributed(self):
        r=distribute_plan(TestPlan(web_users=4,web_urls=["https://x.com"]),[_make_node("n1","10.0.0.1","10.0.0.5"),_make_node("n2","10.0.0.6","10.0.0.10")])
        self.assertEqual(r["n1"].web_users+r["n2"].web_users,4)
    def test_single_node_loopback(self):
        r=distribute_plan(TestPlan(voice_calls=1),[_make_node("n1","10.0.0.1","10.0.0.10")])
        self.assertEqual(len(r["n1"].udp_sessions),2)
    def test_plan_id_propagated(self):
        plan=TestPlan(voice_calls=1)
        r=distribute_plan(plan,[_make_node("n1","10.0.0.1","10.0.0.5"),_make_node("n2","10.0.0.6","10.0.0.10")])
        for np in r.values(): self.assertEqual(np.plan_id,plan.plan_id)
    def test_peer_ips_cross_node(self):
        r=distribute_plan(TestPlan(voice_calls=1),[_make_node("n1","10.0.0.1","10.0.0.2"),_make_node("n2","10.0.0.3","10.0.0.4")])
        s1=r["n1"].udp_sessions[0]; s2=r["n2"].udp_sessions[0]
        self.assertEqual(s1["local_ip"],s2["peer_ip"]); self.assertEqual(s2["local_ip"],s1["peer_ip"])

class TestExport(unittest.TestCase):
    def test_csv_has_header(self):
        text=export_csv(_make_session(3)).decode(); self.assertIn("loss_pct",text); self.assertIn("mos_mos",text)
    def test_csv_row_count(self):
        lines=[l for l in export_csv(_make_session(5)).decode().splitlines() if l.strip()]; self.assertEqual(len(lines),6)
    def test_csv_empty(self):
        lines=export_csv(TestSession(plan=TestPlan())).decode().splitlines(); self.assertEqual(len(lines),1)
    def test_stats_basic(self):
        stats=_compute_stats(_make_session(5).snapshots); self.assertIn("mos_avg",stats); self.assertGreater(stats["count"],0)
    def test_stats_empty(self): self.assertEqual(_compute_stats([]),{})
    def test_stats_loss_range(self):
        stats=_compute_stats(_make_session(10).snapshots); self.assertGreaterEqual(stats["loss_max"],stats["loss_avg"])
    def test_csv_has_session_id(self):
        s=_make_session(2); self.assertIn(s.session_id,export_csv(s).decode())

class TestIPRange(unittest.TestCase):
    def test_single(self): self.assertEqual(_ip_range("10.0.0.1","10.0.0.1"),["10.0.0.1"])
    def test_range(self): self.assertEqual(_ip_range("10.0.0.1","10.0.0.5"),["10.0.0.1","10.0.0.2","10.0.0.3","10.0.0.4","10.0.0.5"])
    def test_20_ips(self): r=_ip_range("192.168.1.101","192.168.1.120"); self.assertEqual(len(r),20); self.assertEqual(r[0],"192.168.1.101"); self.assertEqual(r[-1],"192.168.1.120")

if __name__=="__main__":
    loader=unittest.TestLoader(); suite=unittest.TestSuite()
    for cls in [TestModels,TestSessionStore,TestDistributor,TestExport,TestIPRange]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
