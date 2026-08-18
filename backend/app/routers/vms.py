import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import User, VM
from ..security import get_current_user, hash_password, require_admin

router = APIRouter(prefix="/vms", tags=["vms"])


@router.get("", response_model=list[schemas.VMOut])
def list_vms(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "admin":
        return db.query(VM).order_by(VM.name).all()
    allowed_ids = [a.vm_id for a in user.vm_access]
    return db.query(VM).filter(VM.id.in_(allowed_ids)).order_by(VM.name).all()


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

    out = schemas.VMOut.model_validate(vm)
    return schemas.VMCreated(**out.model_dump(), agent_token=agent_token)


@router.delete("/{vm_id}", status_code=204)
def delete_vm(vm_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    db.delete(vm)
    db.commit()
    log_action(db, admin.email, "vm.delete", target=vm.name)
