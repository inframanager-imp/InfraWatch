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
if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
  echo "pip not found — installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-pip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-pip
  fi
fi

if command -v pip3 >/dev/null 2>&1; then
  pip3 install --quiet websocket-client 2>/dev/null \
    || pip3 install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
elif python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip install --quiet websocket-client 2>/dev/null \
    || python3 -m pip install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
else
  echo "Warning: pip not found and could not be installed — live log streaming will be disabled (heartbeat/inventory still work)." >&2
fi

mkdir -p /opt/infrawatch-agent

cat > /opt/infrawatch-agent/agent.py << 'PYEOF'
#!/usr/bin/env python3
"""InfraWatch agent — reports Docker container and systemd service status.
Outbound-only: this process only ever makes requests to the server; the
server never connects into this VM.
"""
import argparse
import hashlib
import json
import os
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

AGENT_PATH = "/opt/infrawatch-agent/agent.py"
# The install script is this agent's one source of truth — self-update just
# re-reads the exact same heredoc block a fresh install would write, so
# there's never a second copy of the source to drift out of sync.
_SRC_START = "cat > /opt/infrawatch-agent/agent.py << 'PYEOF'\n"
_SRC_END = "\nPYEOF\n"

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


def is_custom_unit(name):
    # A hand-written unit lives directly at /etc/systemd/system/<name> — this
    # is how every OS package instead ships its units under /lib or
    # /usr/lib/systemd/system, and it's also exactly how this agent's own
    # service and every custom app service we've seen gets installed. Pure
    # filesystem check, no subprocess spawned per service.
    try:
        return os.path.isfile("/etc/systemd/system/" + name)
    except Exception:
        return False


def collect_services():
    out = run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"])
    services = []
    for line in out.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            name, _load, active, sub = parts[0], parts[1], parts[2], parts[3]
            services.append({"name": name, "status": active, "sub_state": sub, "custom": is_custom_unit(name)})
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
            cmd = ["docker", "logs", "-f", "--tail", "500", target_name]
        elif target_type == "service":
            cmd = ["journalctl", "-u", target_name, "-f", "-n", "500", "--no-pager"]
        elif target_type == "file":
            # -F (not -f): re-opens the file if it's rotated/recreated,
            # which plain -f would silently stop following after.
            cmd = ["tail", "-F", "-n", "500", target_name]
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


def extract_agent_source(install_script_text):
    """Pulls the exact bytes the heredoc in install-agent.sh would write to
    AGENT_PATH out of the full script text — same source, same output,
    whether it comes from a fresh install or this function."""
    start = install_script_text.index(_SRC_START) + len(_SRC_START)
    end = install_script_text.index(_SRC_END, start) + 1  # +1 keeps the trailing newline the heredoc itself writes
    return install_script_text[start:end]


def check_for_update(server):
    """Fetches install-agent.sh, extracts the embedded agent source, and — only
    if it's valid Python and actually different from what's on disk — replaces
    the running copy and restarts. The syntax check means a broken deploy on
    the server can't crash-loop every agent in the fleet; it just gets skipped
    until the server-side content is fixed."""
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/install-agent.sh", timeout=20) as resp:
            script_text = resp.read().decode("utf-8")
        latest = extract_agent_source(script_text)
    except Exception as e:
        print(f"update check failed: {e}", file=sys.stderr)
        return

    try:
        compile(latest, AGENT_PATH, "exec")
    except SyntaxError as e:
        print(f"update check: fetched source doesn't parse, skipping this cycle: {e}", file=sys.stderr)
        return

    try:
        with open(AGENT_PATH) as f:
            current = f.read()
    except Exception:
        current = ""

    if hashlib.sha256(latest.encode()).hexdigest() == hashlib.sha256(current.encode()).hexdigest():
        return

    print("update check: newer agent version available, updating and restarting")
    tmp_path = AGENT_PATH + ".new"
    with open(tmp_path, "w") as f:
        f.write(latest)
    os.replace(tmp_path, AGENT_PATH)  # atomic — never leaves a half-written agent.py
    subprocess.Popen(["systemctl", "restart", "--no-block", "infrawatch-agent"])
    time.sleep(2)
    sys.exit(0)


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

    update_check_every = max(1, 600 // args.interval)  # roughly every 10 minutes
    tick = 0
    while True:
        send_heartbeat(args.server, args.token, args.name)
        tick += 1
        if tick % update_check_every == 0:
            check_for_update(args.server)
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
