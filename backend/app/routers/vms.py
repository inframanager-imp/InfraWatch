import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import Container, Service, User, VM
from ..security import get_current_user, hash_password, require_admin

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


@router.get("/{vm_id}/containers", response_model=list[schemas.ContainerOut])
def list_containers(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _get_accessible_vm(db, user, vm_id)
    return db.query(Container).filter(Container.vm_id == vm.id).order_by(Container.name).all()


@router.get("/{vm_id}/services", response_model=list[schemas.ServiceOut])
def list_services(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _get_accessible_vm(db, user, vm_id)
    return db.query(Service).filter(Service.vm_id == vm.id).order_by(Service.name).all()


@router.delete("/{vm_id}", status_code=204)
def delete_vm(vm_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    db.delete(vm)
    db.commit()
    log_action(db, admin.email, "vm.delete", target=vm.name)
