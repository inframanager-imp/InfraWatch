import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..models import Container, Service, VM
from ..security import verify_password

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

    # Full replace each heartbeat — simplest correct approach at this scale;
    # revisit with a diff/upsert if container/service counts grow large.
    db.query(Container).filter(Container.vm_id == vm.id).delete()
    db.query(Service).filter(Service.vm_id == vm.id).delete()
    for c in payload.containers:
        db.add(Container(vm_id=vm.id, name=c.name, image=c.image, status=c.status, logs=c.logs))
    for s in payload.services:
        db.add(Service(vm_id=vm.id, name=s.name, status=s.status, sub_state=s.sub_state))

    db.commit()
    return {"status": "ok"}
