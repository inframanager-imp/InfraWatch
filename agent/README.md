# Agent

The actual agent script and its installer live in `backend/app/static/install-agent.sh` — that single self-contained
bash script is served live at `GET /install-agent.sh` (that's how "Add VM" in the UI generates a real, working
install command). It's kept there rather than duplicated in this folder so there's one source of truth.

## What it does

Installed via the command shown in the UI after registering a VM:

```
curl -sSL <server>/install-agent.sh | sudo bash -s -- --server=<url> --token=<token> --name=<vm-name>
```

The installer:
- Detects apt/dnf/yum and installs `python3` if missing
- Writes a small Python agent to `/opt/infrawatch-agent/agent.py` (stdlib only, no pip install needed)
- Installs it as a systemd service (`infrawatch-agent.service`, `Restart=always`)

The agent itself, every 30s (`--interval` to change):
- Runs `docker ps -a` and `systemctl list-units --type=service --all` (via `subprocess`)
- POSTs the results + its token to `POST /agent/heartbeat`, authenticating with the per-VM token issued at
  `POST /vms` (bcrypt-verified server-side against `VM.agent_token_hash` — the plaintext token is never stored)

Connection direction is always agent → server (outbound only, plain HTTPS POST). The server never opens a
connection into a monitored VM. A VM is considered "online" if its last heartbeat is within the last 90 seconds
(`OFFLINE_AFTER_SECONDS` in `backend/app/routers/vms.py`) — computed on read, not polled by a background job.

## Not built yet

- Live log streaming (WebSocket) — heartbeat is periodic snapshot data only, not real-time
- Alerting on state changes (container/service down, VM unreachable) — the data needed for this now exists
  (`last_heartbeat`, container/service status), but nothing evaluates it into alert events yet
- Per-resource monitoring/log/alert enable-disable toggles — visual only in the design mockup, not persisted
- `install-agent.sh` is served at `GET /install-agent.sh`, nested under the `/api` path in production
  (`https://infrawatch.prismxai.com/api/install-agent.sh`) since it's generated from `API_BASE`. Would be cleaner
  as a plain top-level path — needs an nginx tweak to route `/install-agent.sh` straight to the backend, unprefixed.
