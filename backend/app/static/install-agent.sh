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

mkdir -p /opt/infrawatch-agent

cat > /opt/infrawatch-agent/agent.py << 'PYEOF'
#!/usr/bin/env python3
"""InfraWatch agent — reports Docker container and systemd service status.
Outbound-only: this process only ever makes requests to the server; the
server never connects into this VM.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


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


def collect_containers():
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"])
    containers = []
    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            name, image, status = parts
            # Snapshot only — not live streaming. Kept short (--tail) and
            # per-container timeout small so this can't meaningfully delay
            # the heartbeat even with several containers.
            logs = run_logs(["docker", "logs", "--tail", "50", "--timestamps", name])
            containers.append({"name": name, "image": image, "status": status, "logs": logs})
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


def send_heartbeat(server, token, name):
    payload = {
        "name": name,
        "token": token,
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
systemctl enable --now infrawatch-agent

echo "InfraWatch agent installed and started."
echo "Check status: systemctl status infrawatch-agent"
echo "View logs:    journalctl -u infrawatch-agent -f"
