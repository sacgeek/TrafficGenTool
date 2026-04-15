# NetLab Traffic Generator — Deployment Guide

## Architecture Overview

```
Controller VM          Worker Node 1          Worker Node 2
┌─────────────────┐    ┌────────────────┐    ┌────────────────┐
│ FastAPI :8000   │◄───│ agent.py       │    │ agent.py       │
│ React UI :8000  │◄───│ IP aliases     │◄──►│ IP aliases     │
│ WS /ws/agent    │    │ UDP sender     │    │ UDP sender     │
│ WS /ws/dashboard│    │ UDP receiver   │    │ UDP receiver   │
│ REST /api/...   │    │ Playwright     │    │ Playwright     │
└─────────────────┘    └────────────────┘    └────────────────┘
     192.168.1.10           .1.20                  .1.30
                        aliases .101-.120      aliases .121-.140
```

---

## Requirements

### All VMs
- Ubuntu 22.04 / 24.04 (or any modern Linux)
- Python 3.11+
- Git

### Controller VM
- 2 vCPU, 2 GB RAM minimum
- Port 8000 open to lab network

### Worker VMs
- 4–8 vCPU, 8–16 GB RAM (for full 20-user load with web workers)
- Root / `sudo` (required for IP alias provisioning)
- UDP ports 3478–3480 open between worker VMs

---

## Step 1 — Clone and install on all VMs

```bash
git clone https://github.com/yourorg/netlab.git
cd netlab
```

### Controller VM
```bash
pip3 install fastapi uvicorn[standard] pydantic websockets reportlab
```

### Worker VMs
```bash
pip3 install websockets playwright yt-dlp
playwright install chromium          # ~130 MB, one-time
playwright install-deps chromium     # system deps (run as root)
```

---

## Step 2 — Build the React dashboard

On the controller VM (or any machine with Node.js 18+):

```bash
cd netlab/dashboard
npm install
npm run build                        # produces dashboard/dist/
```

The FastAPI server automatically serves the built frontend from `dashboard/dist/`.

For development (hot reload):
```bash
npm run dev                          # runs on :3000, proxies API to :8000
```

---

## Step 3 — Start the controller

```bash
cd netlab
uvicorn controller.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1                      # must be 1 — in-memory state
```

Open `http://<controller-ip>:8000` in a browser.

For production (auto-restart on crash):
```bash
# /etc/systemd/system/netlab-controller.service
[Unit]
Description=NetLab Controller
After=network.target

[Service]
User=netlab
WorkingDirectory=/opt/netlab
ExecStart=/usr/local/bin/uvicorn controller.main:app \
          --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now netlab-controller
```

---

## Step 4 — Configure IP aliases on each worker VM

Each worker needs a pool of IP aliases — one per simulated user.
Choose a range that doesn't conflict with your existing hosts.

```bash
# Example: worker-node-1 gets .101–.120
# worker-node-2 gets .121–.140

# The agent provisions these automatically at test start,
# but you can pre-verify the interface name first:
ip link show                         # note your interface: eth0, ens3, etc.

# Manual test (optional):
sudo ip addr add 192.168.1.101/24 dev eth0
ip addr show eth0                    # confirm it appears
sudo ip addr del 192.168.1.101/24 dev eth0
```

Make sure the worker VMs can reach each other on these addresses:
```bash
# From worker-node-2, ping an alias on worker-node-1:
ping 192.168.1.101
```

---

## Step 5 — Start agents on each worker VM

```bash
# Worker Node 1 (run as root or with CAP_NET_ADMIN)
sudo python3 -m agent.agent \
    --controller ws://192.168.1.10:8000/ws/agent \
    --node-id    worker-node-1 \
    --ip-start   192.168.1.101 \
    --ip-end     192.168.1.120 \
    --interface  eth0

# Worker Node 2
sudo python3 -m agent.agent \
    --controller ws://192.168.1.10:8000/ws/agent \
    --node-id    worker-node-2 \
    --ip-start   192.168.1.121 \
    --ip-end     192.168.1.140 \
    --interface  eth0
```

As soon as the agent connects, it appears in the dashboard's "Worker nodes" panel
and its status changes from OFFLINE → IDLE.

For production (systemd):
```bash
# /etc/systemd/system/netlab-agent.service
[Unit]
Description=NetLab Agent
After=network.target

[Service]
User=root
WorkingDirectory=/opt/netlab
ExecStart=/usr/bin/python3 -m agent.agent \
          --controller ws://192.168.1.10:8000/ws/agent \
          --node-id worker-node-1 \
          --ip-start 192.168.1.101 \
          --ip-end   192.168.1.120 \
          --interface eth0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Step 6 — Run a test from the dashboard

1. Open `http://<controller-ip>:8000`
2. Click **Test Setup** tab
3. Configure:
   - **Voice calls** — bidirectional voice streams (port 3478)
   - **Video calls** — bidirectional video streams (port 3479)
   - **Screen shares** — one-to-many screen share (port 3480)
   - **Web users** — headless browser sessions (enter URLs below)
   - **YouTube streams** — auto-disabled if youtube.com is unreachable
   - **Duration** — seconds, or 0 for manual stop
4. Click **▶ START TEST**
5. Switch to **Live Monitor** tab to see real-time MOS scores, packet loss, latency, jitter

### Stopping a test
- Click **■ STOP** in the Sessions panel, or
- Let the duration timer expire automatically

### Exporting results
In the Sessions panel, each completed session has **CSV** and **PDF** buttons.

---

## Teams port reference

| Stream type  | Port | Protocol | DSCP  | Bitrate    | Direction    |
|--------------|------|----------|-------|------------|--------------|
| Voice        | 3478 | UDP      | EF/46 | ~67 kbps   | Bidirectional|
| Video        | 3479 | UDP      | AF41/34| ~290 kbps | Bidirectional|
| Screen share | 3480 | UDP      | AF21/18| ~320 kbps | One-to-many  |

These match Microsoft Teams' primary STUN/TURN media ports.
For realistic QoS testing, configure your lab switches to honor DSCP markings.

---

## MOS score reference

| Score range | Label     | User experience              |
|-------------|-----------|------------------------------|
| 4.3 – 5.0   | Excellent | Perfect quality              |
| 4.0 – 4.3   | Good      | Slight impairment, acceptable|
| 3.6 – 4.0   | Fair      | Noticeable degradation       |
| 3.1 – 3.6   | Poor      | Significant quality issues   |
| 1.0 – 3.1   | Bad       | Unusable                     |

---

## Network topology tips

### For QoS testing
Apply QoS policies on your lab switch using DSCP markings.
With 2 worker VMs and 20 IPs each, you can test:
- All traffic from one subnet (node-1's .101–.120) getting priority queue
- Voice (EF) vs video (AF41) vs screenshare (AF21) queue treatment
- Per-user bandwidth caps by targeting individual IPs

### For WAN emulation
Use `tc netem` to add artificial latency/loss on the worker interfaces:
```bash
# Simulate 50ms latency + 1% loss on the test interface
sudo tc qdisc add dev eth0 root netem delay 50ms loss 1%

# Remove when done
sudo tc qdisc del dev eth0 root
```

### Single-VM testing (loopback)
You can run both the controller and a single agent on one VM.
UDP sessions will loopback through 127.0.0.1, which is useful for
smoke-testing but won't produce meaningful loss/jitter data.
Use `--ip-start 127.0.0.1 --ip-end 127.0.0.1` and skip IP alias provisioning.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Agent shows OFFLINE | WS URL wrong or firewall | Check controller IP:port, open 8000 |
| IP aliases not created | Not running as root | `sudo python3 -m agent.agent ...` |
| UDP streams show 100% loss | Worker VMs can't reach each other | Verify routing, check firewall rules for UDP 3478-3480 |
| YouTube disabled | No internet access | Expected in isolated labs — web workers still work for local URLs |
| Playwright fails | Missing system deps | Run `playwright install-deps chromium` as root |
| MOS always shows 0 | Clock skew between VMs | Install/sync NTP — latency readings need synced clocks; loss and jitter still work without it |

---

## Development workflow

```bash
# Terminal 1: Controller with hot reload
uvicorn controller.main:app --reload --port 8000

# Terminal 2: React dev server (proxies API calls to :8000)
cd dashboard && npm run dev

# Terminal 3: Agent (dry-run mode, no root needed)
python3 -m agent.agent \
    --controller ws://127.0.0.1:8000/ws/agent \
    --node-id dev-node \
    --ip-start 127.0.0.1 \
    --ip-end   127.0.0.1

# Run all tests
python3 tests/run_tests.py
python3 tests/run_controller_tests.py
```

---

## What's next (phase 3 & 4)

The following features are scaffolded but not yet fully implemented:

- **Web worker** (`agent/workers/web_worker.py`) — Playwright headless browser
  cycling through URLs, reporting page load time and DNS latency as snapshots
- **YouTube worker** (`agent/workers/youtube_worker.py`) — yt-dlp streaming
  with bandwidth and buffering metrics
- **Historical storage** — optional InfluxDB/TimescaleDB integration
  for storing sessions across controller restarts
- **Alert thresholds** — configurable MOS floor that triggers a dashboard
  alarm and logs a warning to the session record
