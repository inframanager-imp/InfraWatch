"""In-memory registry relaying live log lines between agents and browsers.

Single-process only — if this backend is ever scaled to multiple replicas,
this needs to move to something shared (e.g. Redis pub/sub) since each
process would otherwise only see the agent/browser connections it happens
to hold itself. Fine for the current single-VM deployment.
"""
from fastapi import WebSocket

agent_sockets: dict[str, WebSocket] = {}    # vm name -> agent's websocket
browser_sockets: dict[str, WebSocket] = {}  # stream_id -> browser's websocket
