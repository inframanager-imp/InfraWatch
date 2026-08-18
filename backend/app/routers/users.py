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
    return db.query(User).order_by(User.email).all()


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
    return user
