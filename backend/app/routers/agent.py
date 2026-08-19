import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import schemas
from ..database import SessionLocal, get_db
from ..models import Container, ResourceSetting, Service, VM
from ..security import verify_password
from ..streams import agent_sockets, browser_sockets

router = APIRouter(tags=["agent"])

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@router.get("/install-agent.sh", response_class=PlainTextResponse)
def install_script():
    with open(os.path.join(STATIC_DIR, "install-agent.sh")) as f:
        return f.read()


@router.post("/agent/heartbeat")
def heartbeat(payload: schemas.HeartbeatIn, db: Session = Depends(get_db)):
    vm = db.query(VM).filter(VM.name == payload.name).first()
    if not vm or not verify_password(payload.token, vm.agent_token_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid VM name or token")

    vm.last_heartbeat = datetime.utcnow()
    vm.status = "online"
    vm.cpu_percent = payload.cpu_percent
    vm.mem_percent = payload.mem_percent
    vm.disk_percent = payload.disk_percent

    # Full replace each heartbeat — simplest correct approach at this scale;
    # revisit with a diff/upsert if container/service counts grow large.
    db.query(Container).filter(Container.vm_id == vm.id).delete()
    db.query(Service).filter(Service.vm_id == vm.id).delete()
    for c in payload.containers:
        db.add(Container(
            vm_id=vm.id, name=c.name, image=c.image, status=c.status, logs=c.logs,
            cpu_percent=c.cpu_percent, mem_usage=c.mem_usage, restart_count=c.restart_count, ports=c.ports,
        ))
    for s in payload.services:
        db.add(Service(vm_id=vm.id, name=s.name, status=s.status, sub_state=s.sub_state))

    # First-sight default: a service the agent flags as a hand-written unit
    # (not shipped by an OS package) starts Monitor-on; anything else starts
    # Monitor-off, so a fresh VM with hundreds of OS services doesn't need a
    # manual "all off, then re-enable a handful" pass every time. This only
    # ever runs the first time a given service name is seen for this VM —
    # once a ResourceSetting row exists, a user's own toggle always wins.
    known_names = {
        row.name for row in db.query(ResourceSetting.name).filter(
            ResourceSetting.vm_id == vm.id, ResourceSetting.resource_type == "service"
        ).all()
    }
    for s in payload.services:
        if s.custom is not None and s.name not in known_names:
            db.add(ResourceSetting(vm_id=vm.id, resource_type="service", name=s.name, monitor_enabled=s.custom))
            known_names.add(s.name)

    db.commit()
    return {"status": "ok"}


@router.websocket("/agent/ws")
async def agent_ws(websocket: WebSocket, name: str, token: str):
    """Persistent, agent-initiated connection used only for on-demand live
    log streaming — separate from the regular heartbeat POST. The agent
    holds this open; the server pushes start/stop-stream commands down it
    and relays log lines the agent sends back up to whichever browser
    requested them. The agent still only ever dials out to the server."""
    await websocket.accept()

    db = SessionLocal()
    try:
        vm = db.query(VM).filter(VM.name == name).first()
        valid = vm is not None and verify_password(token, vm.agent_token_hash)
    finally:
        db.close()

    if not valid:
        await websocket.close(code=4401)
        return

    agent_sockets[name] = websocket
    try:
        while True:
            data = await websocket.receive_json()
            stream_id = data.get("stream_id")
            browser_ws = browser_sockets.get(stream_id)
            if browser_ws:
                try:
                    await browser_ws.send_text(data.get("line", ""))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        if agent_sockets.get(name) is websocket:
            del agent_sockets[name]
