from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import User, UserVMAccess, VM
from ..security import hash_password, require_admin

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    users = db.query(User).order_by(User.email).all()
    out = []
    for u in users:
        item = schemas.UserOut.model_validate(u)
        item.vm_ids = [a.vm_id for a in u.vm_access]
        out.append(item)
    return out


@router.post("", response_model=schemas.UserOut)
def create_user(payload: schemas.UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="A user with this email already exists")
    if payload.role not in ("admin", "readonly"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'readonly'")

    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.flush()

    for vm_id in payload.vm_ids:
        vm = db.query(VM).filter(VM.id == vm_id).first()
        if vm:
            db.add(UserVMAccess(user_id=user.id, vm_id=vm.id))

    db.commit()
    db.refresh(user)
    log_action(db, admin.email, "user.create", target=user.email)
    out = schemas.UserOut.model_validate(user)
    out.vm_ids = [a.vm_id for a in user.vm_access]
    return out


@router.patch("/{user_id}", response_model=schemas.UserOut)
def update_user(user_id: str, payload: schemas.UserUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.role is not None and payload.role not in ("admin", "readonly"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'readonly'")

    demoting = payload.role == "readonly" and user.role == "admin"
    if demoting and db.query(User).filter(User.role == "admin", User.id != user.id).count() == 0:
        raise HTTPException(status_code=400, detail="Can't demote the last admin")

    if payload.name is not None:
        user.name = payload.name
    if payload.role is not None:
        user.role = payload.role
    if payload.password:
        user.password_hash = hash_password(payload.password)
    if payload.vm_ids is not None:
        db.query(UserVMAccess).filter(UserVMAccess.user_id == user.id).delete()
        for vm_id in payload.vm_ids:
            vm = db.query(VM).filter(VM.id == vm_id).first()
            if vm:
                db.add(UserVMAccess(user_id=user.id, vm_id=vm.id))

    db.commit()
    db.refresh(user)
    log_action(db, admin.email, "user.update", target=user.email)
    out = schemas.UserOut.model_validate(user)
    out.vm_ids = [a.vm_id for a in user.vm_access]
    return out


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if user.role == "admin" and db.query(User).filter(User.role == "admin", User.id != user.id).count() == 0:
        raise HTTPException(status_code=400, detail="Can't delete the last admin")

    db.delete(user)
    db.commit()
    log_action(db, admin.email, "user.delete", target=user.email)
