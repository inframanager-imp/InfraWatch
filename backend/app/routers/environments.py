from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import Environment, User
from ..security import get_current_user, require_admin

router = APIRouter(prefix="/environments", tags=["environments"])


@router.get("", response_model=list[schemas.EnvironmentOut])
def list_environments(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Environment).order_by(Environment.name).all()


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
    return env
