"""In-memory registry relaying live log lines between agents and browsers.

Single-process only — if this backend is ever scaled to multiple replicas,
this needs to move to something shared (e.g. Redis pub/sub) since each
process would otherwise only see the agent/browser connections it happens
to hold itself. Fine for the current single-VM deployment.
"""
from fastapi import WebSocket

agent_sockets: dict[str, WebSocket] = {}    # vm name -> agent's websocket
browser_sockets: dict[str, WebSocket] = {}  # stream_id -> browser's websocket
stream_vms: dict[str, str] = {}             # stream_id -> vm name serving it

# Sent to a browser on an idle stream so the connection keeps showing traffic.
# Proxies (nginx defaults to a 60s read timeout) drop a socket that has gone
# quiet, and a log file with nothing to say is quiet indefinitely. The client
# drops this line rather than printing it.
STREAM_PING = "\x00infrawatch-ping"
KEEPALIVE_SECONDS = 25
