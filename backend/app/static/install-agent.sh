#!/bin/bash
# InfraWatch agent installer.
# Usage: curl -sSL <server>/install-agent.sh | sudo bash -s -- --server=<url> --token=<token> --name=<vm-name>
set -e

SERVER=""
TOKEN=""
NAME=""
INTERVAL="30"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server=*) SERVER="${1#*=}"; shift ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --name=*) NAME="${1#*=}"; shift ;;
    --interval=*) INTERVAL="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$SERVER" || -z "$TOKEN" || -z "$NAME" ]]; then
  echo "Usage: install-agent.sh --server=<url> --token=<token> --name=<vm-name>" >&2
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run as root (use sudo)." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found — installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3
  else
    echo "Could not detect apt/dnf/yum. Install python3 manually and re-run this script." >&2
    exit 1
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Warning: docker not found on this VM — container monitoring will report nothing until it is installed." >&2
fi

# websocket-client is the one dependency the agent needs, and only for the
# optional live-log-streaming channel — heartbeat/inventory reporting has
# zero dependencies and keeps working even if this fails to install.
# Newer Debian/Ubuntu (PEP 668) refuse a plain pip install into the system
# Python at all ("externally-managed-environment"). --break-system-packages
# overrides that — acceptable here since this installs exactly one small,
# well-known package for the agent's own use, not something that risks the
# OS's own Python tooling in practice. Try the normal path first so older
# systems that don't enforce PEP 668 aren't unnecessarily forcing anything.
if command -v pip3 >/dev/null 2>&1; then
  pip3 install --quiet websocket-client 2>/dev/null \
    || pip3 install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
elif python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip install --quiet websocket-client 2>/dev/null \
    || python3 -m pip install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
else
  echo "Warning: pip not found — live log streaming will be disabled (heartbeat/inventory still work)." >&2
fi

mkdir -p /opt/infrawatch-agent

cat > /opt/infrawatch-agent/agent.py << 'PYEOF'
#!/usr/bin/env python3
"""InfraWatch agent — reports Docker container and systemd service status.
Outbound-only: this process only ever makes requests to the server; the
server never connects into this VM.
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket as ws_client  # optional — only needed for live log streaming
except ImportError:
    ws_client = None

# Delta state between heartbeats, for CPU %.
_last_cpu = None

# Live-log-streaming state: stream_id -> subprocess.Popen. Guards concurrent
# writes to the single shared websocket, since multiple stream threads can
# be sending at once.
_active_streams = {}
_ws_send_lock = threading.Lock()


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def run_logs(cmd, timeout=5):
    # docker logs writes the container's stdout and stderr as two separate
    # streams; merge them into one so we don't silently drop stderr output
    # (where most apps put their warnings/errors).
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


def parse_percent(s):
    try:
        return float(s.strip().rstrip("%"))
    except Exception:
        return None


def read_cpu_percent():
    # Delta-since-last-heartbeat, not an instantaneous blocking sample —
    # returns None on the very first heartbeat (no prior sample to diff against).
    global _last_cpu
    try:
        with open("/proc/stat") as f:
            values = list(map(int, f.readline().split()[1:]))
        idle = values[3] + values[4]
        total = sum(values)
        if _last_cpu is None:
            _last_cpu = (total, idle)
            return None
        prev_total, prev_idle = _last_cpu
        _last_cpu = (total, idle)
        d_total, d_idle = total - prev_total, idle - prev_idle
        return round((1 - d_idle / d_total) * 100, 1) if d_total > 0 else None
    except Exception:
        return None


def read_mem_percent():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])
        total, available = info.get("MemTotal"), info.get("MemAvailable")
        return round((1 - available / total) * 100, 1) if total else None
    except Exception:
        return None


def read_disk_percent():
    try:
        usage = shutil.disk_usage("/")
        return round(usage.used / usage.total * 100, 1)
    except Exception:
        return None


def collect_containers():
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"])
    containers = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue
        name, image, status = parts[0], parts[1], parts[2]
        ports = parts[3] if len(parts) > 3 else ""
        containers.append({"name": name, "image": image, "status": status, "ports": ports or None})

    # One bulk call for live resource stats (only returns running containers).
    stats_out = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"], timeout=10)
    stats_by_name = {}
    for line in stats_out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            stats_by_name[parts[0]] = {"cpu": parts[1], "mem": parts[2]}

    for c in containers:
        stat = stats_by_name.get(c["name"], {})
        c["cpu_percent"] = parse_percent(stat.get("cpu", ""))
        c["mem_usage"] = stat.get("mem")
        # Snapshot only — not live streaming. Kept short (--tail) and
        # per-container timeout small so this can't meaningfully delay
        # the heartbeat even with several containers.
        c["logs"] = run_logs(["docker", "logs", "--tail", "50", "--timestamps", c["name"]])
        rc = run(["docker", "inspect", "-f", "{{.RestartCount}}", c["name"]], timeout=5).strip()
        c["restart_count"] = int(rc) if rc.isdigit() else None
    return containers


def collect_services():
    out = run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"])
    services = []
    for line in out.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            name, _load, active, sub = parts[0], parts[1], parts[2], parts[3]
            services.append({"name": name, "status": active, "sub_state": sub})
    return services


def stream_worker(ws, stream_id, cmd):
    """Runs a follow-mode subprocess (docker logs -f / journalctl -f) and
    pushes each new line to the server over the shared websocket, until
    told to stop or the process/connection ends."""
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        _active_streams[stream_id] = proc
        for line in proc.stdout:
            if stream_id not in _active_streams:
                break
            try:
                with _ws_send_lock:
                    ws.send(json.dumps({"stream_id": stream_id, "line": line.rstrip("\n")}))
            except Exception:
                break
    except Exception:
        pass
    finally:
        _active_streams.pop(stream_id, None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


def handle_ws_message(ws, msg):
    try:
        data = json.loads(msg)
    except Exception:
        return
    action = data.get("action")
    stream_id = data.get("stream_id")
    if action == "start_stream":
        target_type, target_name = data.get("type"), data.get("name")
        if target_type == "container":
            cmd = ["docker", "logs", "-f", "--tail", "300", target_name]
        elif target_type == "service":
            cmd = ["journalctl", "-u", target_name, "-f", "-n", "300", "--no-pager"]
        elif target_type == "file":
            # -F (not -f): re-opens the file if it's rotated/recreated,
            # which plain -f would silently stop following after.
            cmd = ["tail", "-F", "-n", "300", target_name]
        else:
            return
        threading.Thread(target=stream_worker, args=(ws, stream_id, cmd), daemon=True).start()
    elif action == "stop_stream":
        proc = _active_streams.pop(stream_id, None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


def run_ws_agent(server, token, name):
    if ws_client is None:
        print("websocket-client not installed — live log streaming disabled (heartbeat/inventory unaffected)", file=sys.stderr)
        return
    ws_url = (
        server.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        + "/agent/ws?name=" + urllib.parse.quote(name) + "&token=" + urllib.parse.quote(token)
    )
    while True:
        try:
            ws = ws_client.create_connection(ws_url, timeout=30)
            print("live-log channel connected")
            while True:
                msg = ws.recv()
                if not msg:
                    break
                handle_ws_message(ws, msg)
        except Exception as e:
            print(f"live-log channel error: {e}", file=sys.stderr)
        time.sleep(10)


def send_heartbeat(server, token, name):
    payload = {
        "name": name,
        "token": token,
        "cpu_percent": read_cpu_percent(),
        "mem_percent": read_mem_percent(),
        "disk_percent": read_disk_percent(),
        "containers": collect_containers(),
        "services": collect_services(),
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        server.rstrip("/") + "/agent/heartbeat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"heartbeat ok: {len(payload['containers'])} containers, {len(payload['services'])} services")
    except urllib.error.URLError as e:
        print(f"heartbeat failed: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    threading.Thread(target=run_ws_agent, args=(args.server, args.token, args.name), daemon=True).start()

    while True:
        send_heartbeat(args.server, args.token, args.name)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
PYEOF

chmod +x /opt/infrawatch-agent/agent.py

cat > /etc/systemd/system/infrawatch-agent.service << SERVICEEOF
[Unit]
Description=InfraWatch monitoring agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 /opt/infrawatch-agent/agent.py --server=$SERVER --token=$TOKEN --name=$NAME --interval=$INTERVAL
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable infrawatch-agent
# restart (not "enable --now") so re-running this script on a VM that
# already has the agent installed actually picks up new agent.py code —
# --now only starts a service if it isn't already running, which made
# re-installs a silent no-op for any already-running process.
systemctl restart infrawatch-agent

echo "InfraWatch agent installed and started."
echo "Check status: systemctl status infrawatch-agent"
echo "View logs:    journalctl -u infrawatch-agent -f"
