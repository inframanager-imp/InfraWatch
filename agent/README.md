# Agent (not built yet — phase 2)

Small process installed on each monitored VM. Responsibilities:

- Send periodic heartbeats to the backend
- Report Docker container inventory/status (via the Docker socket)
- Report systemd service inventory/status (via `systemctl` / `journalctl`)
- Stream logs on demand over WebSocket
- Authenticate using the per-VM token issued by `POST /vms`

Connection direction is always agent → server (outbound only). The server never opens a connection into a monitored VM.
