from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import VM, Environment, User
from ..security import get_current_user, require_admin

router = APIRouter(prefix="/environments", tags=["environments"])


def _with_counts(db: Session, envs: list[Environment]) -> list[schemas.EnvironmentOut]:
    counts = dict(
        db.query(VM.environment_id, func.count(VM.id)).group_by(VM.environment_id).all()
    )
    return [
        schemas.EnvironmentOut(id=e.id, name=e.name, vm_count=counts.get(e.id, 0))
        for e in envs
    ]


@router.get("", response_model=list[schemas.EnvironmentOut])
def list_environments(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return _with_counts(db, db.query(Environment).order_by(Environment.name).all())


@router.post("", response_model=schemas.EnvironmentOut)
def create_environment(
    payload: schemas.EnvironmentCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.query(Environment).filter(Environment.name == payload.name).first():
        raise HTTPException(status_code=400, detail="Environment already exists")
    env = Environment(name=payload.name)
    db.add(env)
    db.commit()
    db.refresh(env)
    log_action(db, admin.email, "environment.create", target=env.name)
    return _with_counts(db, [env])[0]


@router.patch("/{env_id}", response_model=schemas.EnvironmentOut)
def rename_environment(
    env_id: str,
    payload: schemas.EnvironmentCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    env = db.query(Environment).filter(Environment.id == env_id).first()
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required")
    if db.query(Environment).filter(Environment.name == name, Environment.id != env.id).first():
        raise HTTPException(status_code=400, detail="Environment already exists")
    was, env.name = env.name, name
    db.commit()
    db.refresh(env)
    log_action(db, admin.email, "environment.rename", target=name, detail=f"was {was}")
    return _with_counts(db, [env])[0]


@router.delete("/{env_id}", status_code=204)
def delete_environment(env_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    env = db.query(Environment).filter(Environment.id == env_id).first()
    if not env:
        raise HTTPException(status_code=404, detail="Environment not found")
    # Every VM must have an environment, so one still in use cannot be removed
    # without silently moving those VMs somewhere they were never assigned.
    in_use = db.query(VM).filter(VM.environment_id == env.id).count()
    if in_use:
        raise HTTPException(
            status_code=400,
            detail=f"{in_use} VM(s) still use this environment — move them first",
        )
    name = env.name
    db.delete(env)
    db.commit()
    log_action(db, admin.email, "environment.delete", target=name)
