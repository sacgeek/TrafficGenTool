# NetLab Traffic Generator — Validation Test Checklist

Run these tests in order after completing the installation steps in `NetLab-Installation-Guide.docx`. Each test is designed to be self-contained and to build on the infrastructure verified by the previous one.

---

## Phase 1 — Pre-Flight: Automated Unit Tests

Run the full test suite from the `/opt/netlab` directory on the controller VM before touching any live infrastructure.

**Expected result: all 231 tests pass.**

| # | Command | Expected Output |
|---|---------|-----------------|
| 1.1 | `python3 tests/run_tests.py` | 30/30 passed — including a 3-second UDP loopback integration test reporting MOS ≈ 4.38, loss = 0.0% |
| 1.2 | `python3 tests/run_controller_tests.py` | 37/37 passed |
| 1.3 | `python3 tests/run_web_worker_tests.py` | 40/40 passed |
| 1.4 | `python3 tests/run_youtube_worker_tests.py` | 61/61 passed |
| 1.5 | `python3 tests/run_alert_tests.py` | 37/37 passed |
| 1.6 | `python3 tests/run_ntp_tests.py` | 26/26 passed (6 skipped gracefully if pydantic/fastapi absent) |

**Stop here and fix any failures before proceeding.**

---

## Phase 2 — Controller Health

Tests that the FastAPI server is running and responding correctly.

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 2.1 | Controller process running | `ps aux \| grep uvicorn` | Process visible; listening on 0.0.0.0:8000 |
| 2.2 | Health endpoint responds | `curl -s http://192.168.1.10:8000/api/health` | JSON with `"status": "ok"` and a `server_time` float |
| 2.3 | server_time is current | Inspect `server_time` value from 2.2 | Unix timestamp within 5 seconds of `date +%s` on the controller |
| 2.4 | Nodes endpoint responds | `curl -s http://192.168.1.10:8000/api/nodes` | Returns `[]` (empty list — no workers connected yet) |
| 2.5 | Sessions endpoint responds | `curl -s http://192.168.1.10:8000/api/sessions` | Returns `[]` |
| 2.6 | Dashboard UI loads | Open `http://192.168.1.10:8000` in a browser | React dashboard renders; "Live Monitor" and "Test Setup" tabs visible |
| 2.7 | Dashboard WebSocket connects | Observe the header bar status indicator | Status shows "Connected" (not "Disconnected") |

---

## Phase 3 — NTP Clock Synchronisation

Verify that all VMs have synchronised clocks before running any latency-sensitive tests.

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 3.1 | Controller NTP status | `chronyc tracking` on controller | System time offset < 0.050 s; Leap status: Normal |
| 3.2 | Worker 1 NTP status | `chronyc tracking` on worker-node-1 | System time offset < 0.050 s; Leap status: Normal |
| 3.3 | Worker 2 NTP status | `chronyc tracking` on worker-node-2 | System time offset < 0.050 s; Leap status: Normal |
| 3.4 | Manual clock comparison | Run `date` simultaneously on controller and both workers | All three timestamps agree to within 1 second |

---

## Phase 4 — Agent Registration & Clock Delta

Start the agents on each worker VM and verify they register correctly.

| # | Test | Action | Expected Result |
|---|------|--------|-----------------|
| 4.1 | Start agent on worker-node-1 | Run agent with `--node-id worker-node-1 --ip-start 192.168.1.101 --ip-end 192.168.1.120` | Agent log shows "Connected." and "REGISTER" sent |
| 4.2 | Node 1 appears in dashboard | Refresh dashboard / observe Live Monitor | "worker-node-1" appears in Worker Nodes panel with status IDLE |
| 4.3 | Clock delta displayed for node 1 | Observe NodePanel row | "Δt X.X ms" appears below IP range in grey or yellow |
| 4.4 | Clock delta within threshold | Observe colour of clock delta | **Grey** (not yellow) — offset < 50 ms. If yellow, resolve NTP before continuing |
| 4.5 | Start agent on worker-node-2 | Run agent with `--node-id worker-node-2 --ip-start 192.168.1.121 --ip-end 192.168.1.140` | Agent log shows "Connected." and both nodes visible in dashboard |
| 4.6 | Node 2 clock delta | Observe NodePanel row for worker-node-2 | "Δt X.X ms" in grey — offset < 50 ms |
| 4.7 | Nodes API reflects registration | `curl -s http://192.168.1.10:8000/api/nodes` | Returns 2 objects; each has `clock_delta_ms` as a non-null float |

---

## Phase 5 — IP Alias Provisioning

Verify that the agent correctly provisions and tears down IP aliases.

| # | Test | Action | Expected Result |
|---|------|--------|-----------------|
| 5.1 | Start a short test plan | Launch a test with 1 voice call, 30-second duration | Agent logs show "Provisioned N IP aliases" |
| 5.2 | Aliases visible on worker | On worker-node-1: `ip addr show eth0` | Aliases 192.168.1.101 and 192.168.1.121 (or whichever are assigned) visible |
| 5.3 | Aliases removed after test | After the 30-second test completes, run `ip addr show eth0` again | Aliases are gone; interface is clean |

---

## Phase 6 — UDP Traffic (Voice / Video / Screenshare)

Test the core UDP telemetry pipeline end-to-end.

| # | Test | Configuration | Expected Result |
|---|------|---------------|-----------------|
| 6.1 | Baseline voice test | 2 voice calls, 60 s, default alerts | MOS > 4.0, loss < 0.5%, latency < 10 ms, jitter < 3 ms for all streams |
| 6.2 | Video test | 2 video calls, 60 s | MOS > 3.6, streams visible in live table under stream type "video" |
| 6.3 | Screenshare test | 2 screen shares, 60 s | MOS > 3.1, streams visible for both sender and receiver roles |
| 6.4 | Mixed traffic | 2 voice + 1 video + 1 screenshare, 60 s | All stream types visible in live table simultaneously; no crashes |
| 6.5 | Live chart updates | Observe MOSChart during any test | Chart plots MOS values every 2 seconds; lines visible per stream type |
| 6.6 | MOS summary bar | Observe MOSSummaryBar during any test | One large gauge per active stream type; colours match MOS thresholds (green ≥ 4.0, yellow ≥ 3.6, orange ≥ 3.1, red below) |

---

## Phase 7 — Web Workers

Test headless web browsing simulation. Requires outbound internet access from worker VMs.

| # | Test | Configuration | Expected Result |
|---|------|---------------|-----------------|
| 7.1 | Pre-check connectivity | `curl -o /dev/null -sw "%{http_code}" https://example.com` on each worker | Returns 200 |
| 7.2 | Web worker test | 2 web users, URL list: `https://example.com`, 60 s | Streams appear with `stream_type: web`; page_load_ms populated |
| 7.3 | MOS from page load | Observe web stream MOS in live table | MOS > 3.0 for fast-loading URLs; degrades gracefully for slow/failed pages |
| 7.4 | Error rate penalty | Point web URLs at an unreachable host (e.g. `http://10.255.255.1`) | MOS drops to reflect error rate; no agent crash |

---

## Phase 8 — YouTube Workers

Test yt-dlp streaming simulation. Requires outbound internet access and yt-dlp installed.

| # | Test | Action | Expected Result |
|---|------|--------|-----------------|
| 8.1 | Verify yt-dlp installed | `yt-dlp --version` on each worker | Version string printed (e.g. 2024.x.x) |
| 8.2 | YouTube reachability check | Click "Check YouTube" in Test Setup tab | Shows "Reachable" in green (or "Unreachable" if lab has no internet — that is correct behaviour) |
| 8.3 | YouTube worker test | 1 YouTube user, default URL, 60 s | Stream appears with `stream_type: youtube`; throughput > 0 kbps; MOS populated |
| 8.4 | Stall detection | Set YouTube quality to 1080p on a slow link | stall_seconds > 0; MOS degrades below 3.6; no crash |

---

## Phase 9 — Alert Thresholds

Verify the MOS alert system fires and clears correctly.

| # | Test | Configuration | Expected Result |
|---|------|---------------|-----------------|
| 9.1 | Set a tight alert floor | Launch a test with `alert_mos_floor = 4.9`, `alert_window_s = 5` on any traffic type | After 5 seconds, an alert banner appears at the top of the dashboard (no real-world stream achieves MOS 4.9 reliably) |
| 9.2 | Alert banner content | Observe banner | Shows stream type, node ID, current MOS vs floor, and elapsed seconds |
| 9.3 | Alert auto-clears | Stop the test | Banner disappears; session history shows the alert with a `cleared_at` timestamp |
| 9.4 | Alert disabled | Launch a test with `alert_mos_floor = 0` | No alert banner appears regardless of MOS |
| 9.5 | Alert in session export | Export PDF after a test that generated alerts | PDF includes an "Active Alerts" count in the summary section |

---

## Phase 10 — Session Management & Export

| # | Test | Action | Expected Result |
|---|------|--------|-----------------|
| 10.1 | Session appears in history | Complete any test | Session visible in Session History panel with status STOPPED or COMPLETE |
| 10.2 | Prevent duplicate sessions | Try to launch a second test while one is running | API returns 409 Conflict; dashboard shows an error message |
| 10.3 | Stop session mid-run | Click STOP during an active test | Test stops within 5 seconds; node status returns to IDLE |
| 10.4 | Export CSV | Click "Export CSV" on a completed session | CSV file downloads; open in a spreadsheet and verify columns: node_id, session_id, stream_type, timestamp, loss_pct, latency_ms, jitter_ms, mos_mos |
| 10.5 | Export PDF | Click "Export PDF" on a completed session | PDF downloads; contains session summary, per-stream-type MOS averages, and alert count |
| 10.6 | Clear sessions | `DELETE http://192.168.1.10:8000/api/sessions` | Returns `{"cleared": N}`; Session History panel is empty; node statuses unchanged |

---

## Phase 11 — Resilience & Reconnection

| # | Test | Action | Expected Result |
|---|------|--------|-----------------|
| 11.1 | Agent reconnect | Stop and restart the agent on worker-node-1 while the controller is running | Agent reconnects within 10 seconds; node reappears in dashboard with a fresh clock_delta_ms reading |
| 11.2 | Controller restart | Restart uvicorn / the controller service while agents are running | Agents reconnect automatically (exponential backoff 2 s → 60 s); both nodes reappear in dashboard within 60 s of controller coming back |
| 11.3 | Node timeout | Kill the agent process on one node without stopping gracefully | After 45 s, dashboard marks that node as OFFLINE; other node continues reporting |
| 11.4 | In-flight test survives controller blip | Start a 5-minute test, restart the controller after 30 s | After reconnect, agents continue sending SNAPSHOT messages; telemetry resumes in dashboard |

---

## Phase 12 — Performance Baseline

Run under realistic load to establish a clean-network baseline for your specific lab.

| # | Test | Configuration | Record For Baseline |
|---|------|---------------|---------------------|
| 12.1 | Clean LAN baseline | 5 voice + 3 video + 2 screenshare, 300 s | Avg MOS per stream type; avg loss %; avg latency ms |
| 12.2 | Web browsing baseline | 5 web users, 5 URLs, 300 s | Avg page load ms; avg MOS |
| 12.3 | YouTube baseline | 2 YouTube users, 720p, 300 s | Avg throughput kbps; avg stall_s; avg MOS |
| 12.4 | Max concurrent streams | As many users as node capacity allows | Note the snapshot rate; confirm no snapshots are dropped (check MAX_SNAPSHOTS_PER_SESSION) |

Export the CSV from each baseline run and save it. These are your reference values for interpreting future degradation tests.

---

## Sign-Off

| Check | Status |
|-------|--------|
| All 231 automated tests pass | ☐ |
| NTP offset < 50 ms on all VMs | ☐ |
| Both nodes register with grey clock delta | ☐ |
| UDP voice/video/screenshare traffic flows end-to-end | ☐ |
| Web workers functional (if internet available) | ☐ |
| YouTube workers functional (if internet available) | ☐ |
| Alert system fires and clears correctly | ☐ |
| CSV and PDF export working | ☐ |
| Agent reconnect tested | ☐ |
| Clean-network baseline recorded | ☐ |

Once all items are checked, the system is ready for degradation testing (e.g., using manual `tc netem` commands to inject loss and delay on worker interfaces).
