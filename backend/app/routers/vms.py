import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import SessionLocal, get_db
from ..models import Container, LogSource, ResourceSetting, Service, User, VM
from ..security import get_current_user, get_user_from_token, hash_password, require_admin
from ..streams import agent_sockets, browser_sockets

router = APIRouter(prefix="/vms", tags=["vms"])

OFFLINE_AFTER_SECONDS = 90  # 3 missed heartbeats at the agent's default 30s interval


def _effective_status(vm: VM) -> str:
    if vm.last_heartbeat is None:
        return "pending"
    if datetime.utcnow() - vm.last_heartbeat > timedelta(seconds=OFFLINE_AFTER_SECONDS):
        return "offline"
    return "online"


def _serialize_vm(vm: VM) -> schemas.VMOut:
    out = schemas.VMOut.model_validate(vm)
    out.status = _effective_status(vm)
    return out


def _get_accessible_vm(db: Session, user: User, vm_id: str) -> VM:
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if user.role != "admin":
        allowed_ids = {a.vm_id for a in user.vm_access}
        if vm.id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Not authorized for this VM")
    return vm


@router.get("", response_model=list[schemas.VMOut])
def list_vms(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "admin":
        vms = db.query(VM).order_by(VM.name).all()
    else:
        allowed_ids = [a.vm_id for a in user.vm_access]
        vms = db.query(VM).filter(VM.id.in_(allowed_ids)).order_by(VM.name).all()
    return [_serialize_vm(vm) for vm in vms]


@router.post("", response_model=schemas.VMCreated)
def create_vm(payload: schemas.VMCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(VM).filter(VM.name == payload.name).first():
        raise HTTPException(status_code=400, detail="A VM with this name already exists")

    # Shown once at creation time, only the bcrypt hash is persisted — mirrors the
    # one-time install-command flow (agent authenticates with this token going forward).
    agent_token = "tok_" + secrets.token_hex(16)
    vm = VM(
        name=payload.name,
        hostname=payload.hostname,
        ip_address=payload.ip_address,
        environment_id=payload.environment_id,
        agent_token_hash=hash_password(agent_token),
    )
    db.add(vm)
    db.commit()
    db.refresh(vm)
    log_action(db, admin.email, "vm.create", target=vm.name)

    out = _serialize_vm(vm)
    return schemas.VMCreated(**out.model_dump(), agent_token=agent_token)


@router.patch("/{vm_id}", response_model=schemas.VMOut)
def update_vm(vm_id: str, payload: schemas.VMUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")

    if payload.name is not None and payload.name != vm.name:
        if db.query(VM).filter(VM.name == payload.name, VM.id != vm.id).first():
            raise HTTPException(status_code=400, detail="A VM with this name already exists")
        # The agent authenticates its heartbeat by this exact name (see
        # /agent/heartbeat) — renaming here does NOT touch the already-
        # running agent, so it'll start failing to report until reinstalled
        # with --name matching the new value. The caller is responsible for
        # surfacing that; this endpoint just performs the rename.
        vm.name = payload.name
    if payload.hostname is not None:
        vm.hostname = payload.hostname
    if payload.ip_address is not None:
        vm.ip_address = payload.ip_address
    if payload.environment_id is not None:
        vm.environment_id = payload.environment_id

    db.commit()
    db.refresh(vm)
    log_action(db, admin.email, "vm.update", target=vm.name)
    return _serialize_vm(vm)


def _settings_map(db: Session, vm_id: str, resource_type: str) -> dict[str, ResourceSetting]:
    rows = db.query(ResourceSetting).filter(
        ResourceSetting.vm_id == vm_id, ResourceSetting.resource_type == resource_type
    ).all()
    return {row.name: row for row in rows}


@router.get("/{vm_id}/containers", response_model=list[schemas.ContainerOut])
def list_containers(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _get_accessible_vm(db, user, vm_id)
    containers = db.query(Container).filter(Container.vm_id == vm.id).order_by(Container.name).all()
    settings = _settings_map(db, vm.id, "container")
    out = []
    for c in containers:
        item = schemas.ContainerOut.model_validate(c)
        s = settings.get(c.name)
        if s:
            item.monitor_enabled = s.monitor_enabled
            item.logs_enabled = s.logs_enabled
        out.append(item)
    return out


@router.get("/{vm_id}/services", response_model=list[schemas.ServiceOut])
def list_services(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _get_accessible_vm(db, user, vm_id)
    services = db.query(Service).filter(Service.vm_id == vm.id).order_by(Service.name).all()
    settings = _settings_map(db, vm.id, "service")
    out = []
    for s_row in services:
        item = schemas.ServiceOut.model_validate(s_row)
        s = settings.get(s_row.name)
        if s:
            item.monitor_enabled = s.monitor_enabled
            item.logs_enabled = s.logs_enabled
        out.append(item)
    return out


@router.put("/{vm_id}/resource-settings", response_model=schemas.ResourceSettingOut)
def update_resource_setting(vm_id: str, payload: schemas.ResourceSettingUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if payload.resource_type not in ("container", "service"):
        raise HTTPException(status_code=400, detail="resource_type must be 'container' or 'service'")

    setting = db.query(ResourceSetting).filter(
        ResourceSetting.vm_id == vm.id,
        ResourceSetting.resource_type == payload.resource_type,
        ResourceSetting.name == payload.name,
    ).first()
    if not setting:
        setting = ResourceSetting(vm_id=vm.id, resource_type=payload.resource_type, name=payload.name)
        db.add(setting)
    if payload.monitor_enabled is not None:
        setting.monitor_enabled = payload.monitor_enabled
    if payload.logs_enabled is not None:
        setting.logs_enabled = payload.logs_enabled
    db.commit()
    db.refresh(setting)
    log_action(db, admin.email, "resource_setting.update", target=f"{vm.name}:{payload.resource_type}:{payload.name}")
    return setting


@router.put("/{vm_id}/resource-settings/bulk", status_code=204)
def bulk_update_resource_settings(vm_id: str, payload: schemas.ResourceSettingBulkUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Sets one field (monitor_enabled/logs_enabled) to the same value for every
    currently-known container/service on this VM in a single transaction —
    some VMs report hundreds of systemd services, so this must not be a loop
    of one HTTP request per resource from the frontend."""
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if payload.resource_type not in ("container", "service"):
        raise HTTPException(status_code=400, detail="resource_type must be 'container' or 'service'")
    if payload.field not in ("monitor_enabled", "logs_enabled"):
        raise HTTPException(status_code=400, detail="field must be 'monitor_enabled' or 'logs_enabled'")

    model = Container if payload.resource_type == "container" else Service
    names = [row[0] for row in db.query(model.name).filter(model.vm_id == vm.id).all()]

    existing = {
        row.name: row for row in db.query(ResourceSetting).filter(
            ResourceSetting.vm_id == vm.id, ResourceSetting.resource_type == payload.resource_type
        ).all()
    }
    for name in names:
        setting = existing.get(name)
        if not setting:
            setting = ResourceSetting(vm_id=vm.id, resource_type=payload.resource_type, name=name)
            db.add(setting)
        setattr(setting, payload.field, payload.value)

    db.commit()
    log_action(db, admin.email, "resource_setting.bulk_update", target=f"{vm.name}:{payload.resource_type}:{payload.field}={payload.value}")


@router.get("/{vm_id}/log-sources", response_model=list[schemas.LogSourceOut])
def list_log_sources(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _get_accessible_vm(db, user, vm_id)
    return db.query(LogSource).filter(LogSource.vm_id == vm.id).order_by(LogSource.name).all()


@router.post("/{vm_id}/log-sources", response_model=schemas.LogSourceOut)
def create_log_source(vm_id: str, payload: schemas.LogSourceCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    source = LogSource(vm_id=vm.id, name=payload.name, path=payload.path)
    db.add(source)
    db.commit()
    db.refresh(source)
    log_action(db, admin.email, "log_source.create", target=f"{vm.name}:{payload.path}")
    return source


@router.delete("/{vm_id}/log-sources/{source_id}", status_code=204)
def delete_log_source(vm_id: str, source_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    source = db.query(LogSource).filter(LogSource.id == source_id, LogSource.vm_id == vm_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Log source not found")
    db.delete(source)
    db.commit()
    log_action(db, admin.email, "log_source.delete", target=f"{vm_id}:{source.path}")


@router.websocket("/{vm_id}/logs/ws")
async def logs_ws(websocket: WebSocket, vm_id: str, token: str, type: str, name: str):
    await websocket.accept()

    db = SessionLocal()
    try:
        user = get_user_from_token(token, db)
        if not user:
            await websocket.close(code=4401)
            return
        vm = db.query(VM).filter(VM.id == vm_id).first()
        if not vm:
            await websocket.close(code=4404)
            return
        if user.role != "admin":
            allowed_ids = {a.vm_id for a in user.vm_access}
            if vm.id not in allowed_ids:
                await websocket.close(code=4403)
                return
        vm_name = vm.name
    finally:
        db.close()

    agent_ws = agent_sockets.get(vm_name)
    if agent_ws is None:
        await websocket.send_text("[agent not connected]")
        await websocket.close()
        return

    stream_id = str(uuid.uuid4())
    browser_sockets[stream_id] = websocket
    try:
        await agent_ws.send_json({"action": "start_stream", "stream_id": stream_id, "type": type, "name": name})
    except Exception:
        browser_sockets.pop(stream_id, None)
        await websocket.close()
        return

    try:
        while True:
            await websocket.receive_text()  # browser sends nothing meaningful; just blocks until disconnect
    except WebSocketDisconnect:
        pass
    finally:
        browser_sockets.pop(stream_id, None)
        try:
            await agent_ws.send_json({"action": "stop_stream", "stream_id": stream_id})
        except Exception:
            pass


@router.delete("/{vm_id}", status_code=204)
def delete_vm(vm_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    db.delete(vm)
    db.commit()
    log_action(db, admin.email, "vm.delete", target=vm.name)
